import pytest
from typer.testing import CliRunner

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
