import sys

import pytest

from rtl_agent.tools import MissingToolError, parse_diagnostics, require_tools, run_command


def run_python(tmp_path, code, *, stage="test", timeout=2, max_log_bytes=4096):
    return run_command(
        [sys.executable, "-c", code],
        stage=stage,
        cwd=tmp_path,
        timeout=timeout,
        max_log_bytes=max_log_bytes,
    )


def test_timeout_terminates_hanging_process(tmp_path):
    result = run_python(tmp_path, "import time; time.sleep(60)", timeout=0.1)
    assert result.timed_out and not result.passed
    assert result.duration_seconds < 5


def test_timeout_preserves_partial_output(tmp_path):
    result = run_python(
        tmp_path,
        "import time; print('partial output', flush=True); time.sleep(60)",
        timeout=0.2,
    )
    assert result.timed_out and "partial output" in result.output
    assert any("timed out" in d.message for d in result.diagnostics)


def test_os_error_is_recorded(tmp_path):
    result = run_command(
        [str(tmp_path / "missing-tool")],
        stage="lint",
        cwd=tmp_path,
        timeout=1,
        max_log_bytes=4096,
    )
    assert not result.passed and result.returncode is None
    assert "Could not run missing-tool" in result.output


def test_large_output_is_truncated_after_process_exits(tmp_path):
    result = run_python(tmp_path, "print('x' * 8000)")
    assert result.output_limited and not result.passed
    assert result.returncode == 0
    assert len(result.output.encode()) == 4096


@pytest.mark.parametrize(
    "code",
    [
        "pass",
        "print('RTL_AGENT_PASS extra')",
        "print('RTL_AGENT_PASS'); print('RTL_AGENT_PASS')",
        "print('RTL_AGENT_PASS'); raise SystemExit(1)",
        "print('RTL_AGENT_PASS'); print('ERROR: tb.sv:8: bad check')",
    ],
)
def test_simulation_requires_unambiguous_success(tmp_path, code):
    assert not run_python(tmp_path, code, stage="simulate").passed


def test_simulation_accepts_marker_and_zero_exit(tmp_path):
    assert run_python(tmp_path, "print('RTL_AGENT_PASS')", stage="simulate").passed


def test_icarus_13_finish_notice_is_informational(tmp_path):
    result = run_python(
        tmp_path,
        "print('RTL_AGENT_PASS'); print('tb.sv:39: $finish called at 276000 (1ps)')",
        stage="simulate",
    )
    assert result.passed
    assert result.diagnostics[0].severity == "info"


def test_eda_process_does_not_receive_api_credentials(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "secret-do-not-forward")
    monkeypatch.setenv("RTL_AGENT_LIVE_PASSWORD", "private-live-access")
    monkeypatch.setenv("OTHER_SECRET", "private-other-secret")
    result = run_python(
        tmp_path,
        "import os; print(any(k in os.environ for k in "
        "['OPENAI_API_KEY', 'RTL_AGENT_LIVE_PASSWORD', 'OTHER_SECRET']))",
    )
    assert result.output.strip() == "False"


def test_missing_binary_has_actionable_message(monkeypatch):
    monkeypatch.setattr("rtl_agent.tools.shutil.which", lambda name: None)
    with pytest.raises(MissingToolError, match="brew install"):
        require_tools()


def test_diagnostics_preserve_source_locations():
    diagnostics = parse_diagnostics(
        "%Warning-WIDTHEXPAND: npu_neural_mac.sv:12:4: Operator ADD expects 8 bits.\n"
        "npu_neural_mac.sv:21: syntax error\nFATAL: tb.sv:33: MAC mismatch",
        "test",
    )
    assert [(d.file, d.line) for d in diagnostics] == [
        ("npu_neural_mac.sv", 12),
        ("npu_neural_mac.sv", 21),
        ("tb.sv", 33),
    ]
    assert diagnostics[0].code == "WIDTHEXPAND"
