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
