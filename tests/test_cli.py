import json

import pytest
from typer.testing import CliRunner

from rtl_agent.agent import load_report
from rtl_agent.cli import app

runner = CliRunner()


def test_help_lists_workflows():
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert all(command in result.output for command in ("generate", "doctor", "inspect"))


def test_missing_model_has_clear_error(monkeypatch):
    monkeypatch.delenv("RTL_AGENT_MODEL", raising=False)
    result = runner.invoke(app, ["generate", "a signed INT8 NPU MAC"])
    assert result.exit_code == 2
    assert "model" in result.output


def test_spec_input_must_be_unambiguous():
    result = runner.invoke(app, ["generate", "NPU MAC", "--spec-file", "other.txt"])
    assert result.exit_code == 2
    assert "exactly one" in result.output


@pytest.mark.eda
@pytest.mark.parametrize("broken", [False, True])
def test_generate_exit_codes_with_real_eda(
    tmp_path, eda_tools, monkeypatch, provider, design, broken
):
    monkeypatch.setattr("rtl_agent.cli.OpenAIProvider", lambda *args, **kwargs: provider)
    if broken:
        design.rtl = design.rtl.replace("acc +", "acc -")
    result = runner.invoke(
        app,
        [
            "generate",
            "NPU MAC",
            "--model",
            "test-model",
            "--top",
            "npu_neural_mac",
            "--output",
            str(tmp_path),
            "--max-attempts",
            "1",
        ],
    )
    assert result.exit_code == (1 if broken else 0), result.output
    assert ("budget exhausted" if broken else "PASSED") in result.output
    provider.caption.assert_not_called()
    report = json.loads(next(tmp_path.glob("*/report.json")).read_text())
    assert report["caption_pending"] is False
    assert report["caption"] is None and report["caption_source"] is None


@pytest.mark.eda
def test_tb_file_runs_reference_and_reports_synthesis(
    tmp_path, eda_tools, monkeypatch, provider, design
):
    monkeypatch.setattr("rtl_agent.cli.OpenAIProvider", lambda *args, **kwargs: provider)
    reference = tmp_path / "reference.sv"
    reference.write_text(design.testbench)
    result = runner.invoke(
        app,
        [
            "generate",
            "NPU MAC",
            "--model",
            "test-model",
            "--top",
            "npu_neural_mac",
            "--tb-file",
            str(reference),
            "--output",
            str(tmp_path / "runs"),
        ],
    )
    assert result.exit_code == 0, result.output
    assert "Simulation" in result.output and "Synthesis" in result.output
    provider.generate.assert_called_once_with("NPU MAC", "npu_neural_mac", reference.read_text())
    report = load_report(next((tmp_path / "runs").iterdir()))
    assert report.synthesis.flip_flops == 20
    assert report.testbench_sha256


def test_missing_tb_file_fails_before_constructing_provider(tmp_path, monkeypatch):
    def unexpected(*args, **kwargs):
        pytest.fail("No model request should be needed")

    monkeypatch.setattr("rtl_agent.cli.OpenAIProvider", unexpected)
    result = runner.invoke(
        app,
        [
            "generate",
            "NPU MAC",
            "--model",
            "test-model",
            "--tb-file",
            str(tmp_path / "missing"),
        ],
    )
    assert result.exit_code == 2
    assert "missing" in result.output


def test_doctor_checks_yosys_version_and_install_hint(monkeypatch):
    from rtl_agent.tools import run_command

    commands = []

    def version(command, **kwargs):
        commands.append(command)
        return run_command(["/usr/bin/true"], **kwargs)

    monkeypatch.setattr("rtl_agent.cli.tool_paths", lambda: {"yosys": "/usr/bin/yosys"})
    monkeypatch.setattr("rtl_agent.cli.run_command", version)
    assert runner.invoke(app, ["doctor"]).exit_code == 0
    assert commands == [["/usr/bin/yosys", "-V"]]
    monkeypatch.setattr("rtl_agent.cli.tool_paths", lambda: {"yosys": None})
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 2
    assert "brew install icarus-verilog verilator yosys" in result.output


@pytest.mark.parametrize("value,expected", [(None, "12000"), ("32768", "32768")])
def test_doctor_reports_web_output_token_budget(monkeypatch, value, expected):
    monkeypatch.setattr("rtl_agent.cli.tool_paths", lambda: {})
    monkeypatch.delenv("RTL_AGENT_MAX_OUTPUT_TOKENS", raising=False)
    if value is not None:
        monkeypatch.setenv("RTL_AGENT_MAX_OUTPUT_TOKENS", value)
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 0
    assert "RTL_AGENT_MAX_OUTPUT_TOKENS" in result.output
    assert expected in result.output


def test_doctor_rejects_invalid_token_budget_without_echoing_value(monkeypatch):
    monkeypatch.setattr("rtl_agent.cli.tool_paths", lambda: {})
    monkeypatch.setenv("RTL_AGENT_MAX_OUTPUT_TOKENS", "invalid-private-value")
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 2
    assert "1024" in result.output and "65536" in result.output
    assert "invalid-private-value" not in result.output


def test_serve_rejects_non_loopback_binding():
    result = runner.invoke(app, ["serve", "--host", "0.0.0.0"])
    assert result.exit_code == 2
    assert "localhost" in result.output


def test_serve_runs_one_local_server_without_access_logging(monkeypatch):
    pytest.importorskip("fastapi")
    uvicorn = pytest.importorskip("uvicorn")

    calls = []
    monkeypatch.setattr(uvicorn, "run", lambda *args, **kwargs: calls.append(kwargs))
    result = runner.invoke(app, ["serve", "--port", "8123"])
    assert result.exit_code == 0, result.output
    assert calls == [
        {"host": "127.0.0.1", "port": 8123, "access_log": False, "proxy_headers": False}
    ]


def test_hosted_server_requires_explicit_secrets_and_domains(monkeypatch):
    pytest.importorskip("fastapi")
    monkeypatch.delenv("RTL_AGENT_PUBLIC_HOST", raising=False)
    result = runner.invoke(app, ["serve-hosted"])
    assert result.exit_code == 2
    assert "RTL_AGENT_PUBLIC_HOST" in result.output


def test_hosted_server_runs_one_worker_only_with_valid_settings(monkeypatch, tmp_path):
    pytest.importorskip("fastapi")
    uvicorn = pytest.importorskip("uvicorn")
    monkeypatch.setenv("RTL_AGENT_PUBLIC_HOST", "backend.hf.space")
    monkeypatch.setenv("RTL_AGENT_ALLOWED_ORIGINS", "https://site.vercel.app")
    monkeypatch.setenv("RTL_AGENT_LIVE_PASSWORD", "test-password-for-invited-visitors")
    monkeypatch.setenv("RTL_AGENT_RUNS_DIR", str(tmp_path))
    calls = []
    monkeypatch.setattr(uvicorn, "run", lambda *args, **kwargs: calls.append(kwargs))
    result = runner.invoke(app, ["serve-hosted", "--port", "8124"])
    assert result.exit_code == 0, result.output
    assert calls == [
        {"host": "0.0.0.0", "port": 8124, "access_log": False, "proxy_headers": False, "workers": 1}
    ]
    assert "test-password-for-invited-visitors" not in result.output
