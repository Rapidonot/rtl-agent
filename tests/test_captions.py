import copy
import re
import threading
from unittest.mock import Mock

import pytest

from rtl_agent.captions import caption_facts, failure_kind, fallback_caption, start_caption
from rtl_agent.models import Attempt, Diagnostic, RunReport
from rtl_agent.providers import Provider


def make_report(tmp_path):
    return RunReport(
        run_id="20260101T010101Z-12345678",
        provider="mock",
        model="test",
        spec="Design an 8-bit population-counter.",
        top="dut",
        started_at="2026-01-01T01:01:01+00:00",
        run_dir=str(tmp_path),
        status="passed",
        summary="This metadata must not enter the caption prompt.",
        assumptions=["Not caption facts"],
        attempts=[
            Attempt(
                number=1,
                rtl_sha256="a",
                testbench_sha256="b",
                change_summary="Initial.",
                passed=False,
                tools=[],
                diagnostics=[
                    Diagnostic(
                        tool="simulate",
                        severity="error",
                        message="mismatch expected=-18 actual=65518",
                    )
                ],
            ),
            Attempt(
                number=2,
                rtl_sha256="c",
                testbench_sha256="b",
                change_summary="Repair sum.",
                passed=True,
                tools=[],
            ),
        ],
    )


def test_caption_uses_only_first_failure_next_summary_and_final_status(tmp_path):
    report = make_report(tmp_path)
    facts = caption_facts(report)
    assert set(facts) == {
        "spec",
        "diagnostics",
        "change_summary",
        "attempt_number",
        "total_attempts",
        "final_status",
        "later_attempt_passed",
        "failure_kind",
    }
    assert facts["change_summary"] == "Repair sum."
    assert facts["diagnostics"][0]["message"] == "mismatch expected=-18 actual=65518"
    assert "65,518" in fallback_caption(facts)
    assert facts["attempt_number"] == 1 and facts["total_attempts"] == 2
    assert facts["final_status"] == "passed" and facts["later_attempt_passed"]
    assert facts["failure_kind"] == "wrong_answer"


@pytest.mark.parametrize(
    "diagnostics,expected",
    [
        ([{"tool": "simulate", "severity": "error", "message": "mismatch"}], "wrong_answer"),
        (
            [{"tool": "simulate", "severity": "error", "message": "assertion failed"}],
            "wrong_answer",
        ),
        ([{"tool": "compile", "severity": "error", "message": "unexpected token"}], "wont_build"),
        ([{"tool": "lint", "severity": "error", "message": "syntax error"}], "wont_build"),
        (
            [{"tool": "lint", "severity": "warning", "message": "missing final newline"}],
            "housekeeping",
        ),
        (
            [{"tool": "compile", "severity": "warning", "message": "inherited timescale"}],
            "housekeeping",
        ),
        ([{"tool": "lint", "severity": "warning", "message": "unused signal"}], "housekeeping"),
        ([{"tool": "lint", "severity": "warning", "message": "inferred latch"}], "unknown"),
        ([{"tool": "lint", "severity": "warning", "message": "wrong width"}], "unknown"),
        ([{"tool": "simulate", "severity": "error", "message": "watchdog timeout"}], "timeout"),
        ([], "unknown"),
    ],
)
def test_failure_kind_is_computed_from_evidence(diagnostics, expected):
    assert failure_kind(diagnostics) == expected


def test_promoted_warning_summary_does_not_turn_formatting_into_a_wrong_answer():
    diagnostics = [
        {"tool": "lint", "severity": "warning", "code": "EOFNEWLINE", "message": "Missing newline"},
        {"tool": "lint", "severity": "error", "message": "%Error: Exiting due to 1 warning(s)"},
    ]
    assert failure_kind(diagnostics) == "housekeeping"
    assert failure_kind(diagnostics, timed_out=True) == "timeout"
    diagnostics.append({"tool": "simulate", "severity": "error", "message": "mismatch"})
    assert failure_kind(diagnostics) == "wrong_answer"


@pytest.mark.parametrize("signal", ["assertion_error", "watchdog_timeout", "mismatch"])
def test_unused_signal_names_do_not_invent_an_answer_failure_or_timeout(signal):
    diagnostics = [{"tool": "lint", "severity": "warning", "message": f"unused signal {signal}"}]
    assert failure_kind(diagnostics) == "housekeeping"


def test_substantive_warning_code_is_not_hidden_by_a_benign_signal_name():
    diagnostics = [
        {
            "tool": "lint",
            "severity": "warning",
            "code": "LATCH",
            "message": "Latch inferred for signal unused_timescale_flag",
        }
    ]
    assert failure_kind(diagnostics) == "unknown"


@pytest.mark.parametrize(
    "kind", ["wrong_answer", "housekeeping", "wont_build", "timeout", "unknown"]
)
@pytest.mark.parametrize("later_passed", [False, True])
def test_fallback_is_two_plain_sentences_and_only_claims_a_recorded_fix(
    tmp_path, kind, later_passed
):
    facts = caption_facts(make_report(tmp_path))
    facts.update(
        failure_kind=kind,
        final_status="passed" if later_passed else "failed",
        later_attempt_passed=later_passed,
    )
    caption = fallback_caption(facts)
    assert len(re.findall(r"[.!?](?:\s|$)", caption)) == 2
    assert "bit-counter" in caption
    assert not re.search(
        r"\b(lint|compile|compilation|timescale|testbench|reference model|assertion|RTL|"
        r"Verilog|structural|diagnostic)\b",
        caption,
        re.I,
    )
    assert ("fixed it" in caption) is later_passed
    if kind == "housekeeping":
        assert "formatting slip, not a wrong answer" in caption
    if not later_passed:
        assert "None of the attempts passed every check" in caption


def test_later_attempt_counts_and_pass_without_a_failure_are_not_confused(tmp_path):
    report = make_report(tmp_path)
    report.attempts.insert(0, report.attempts[0].model_copy(update={"number": 1}))
    report.attempts[1].number, report.attempts[2].number = 2, 3
    assert "Attempt 3 fixed it" in fallback_caption(caption_facts(report))
    report.attempts = [report.attempts[-1].model_copy(update={"number": 1})]
    caption = fallback_caption(caption_facts(report))
    assert "first draft" in caption and "No repair was needed" in caption
    assert "fixed it" not in caption


def test_caption_timeout_returns_fallback_and_discards_late_text(tmp_path):
    report = make_report(tmp_path)
    report.caption = fallback_caption(caption_facts(report))
    report.caption_source, report.caption_pending = "deterministic", True
    hold = threading.Event()
    provider = Mock(spec=Provider)
    provider.caption.side_effect = lambda _: (hold.wait(2), "Late caption.")[1]
    saves = []
    thread = start_caption(provider, report, lambda r: saves.append(copy.deepcopy(r)), timeout=0.02)
    assert report.status == "passed"
    thread.join(1)
    assert not thread.is_alive()
    assert report.caption_source == "deterministic" and not report.caption_pending
    assert len(saves) == 1 and "65,518" in saves[0].caption
    hold.set()
    provider.caption.assert_called_once()


def test_caption_success_changes_only_explanation_fields(tmp_path):
    report = make_report(tmp_path)
    before = report.model_dump()
    provider = Mock(spec=Provider)
    provider.caption.return_value = "The test caught an incorrect sum. The next attempt passed."
    thread = start_caption(provider, report, lambda _: None)
    thread.join(1)
    after = report.model_dump()
    for key in ("caption", "caption_source", "caption_pending"):
        before.pop(key)
        after.pop(key)
    assert before == after
    assert report.caption_source == "model"
