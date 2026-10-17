"""Optional explanations. Captions cannot change a verification result."""

import queue
import re
import threading
from collections.abc import Callable

from rtl_agent.models import RunReport
from rtl_agent.providers import Provider


def failure_kind(diagnostics: list[dict], *, timed_out: bool = False) -> str:
    """Classify evidence conservatively; this never changes verification fields."""
    issues = [d for d in diagnostics if d["severity"] != "info"]
    errors = [d for d in issues if d["severity"] == "error"]
    messages = " ".join(d["message"] for d in errors)
    if timed_out or re.search(r"\btimed?\s*out\b|\btimeout\b", messages, re.I):
        return "timeout"
    if any(
        d["severity"] == "error"
        and (
            d["tool"] in ("compile", "iverilog")
            or re.search(r"syntax|parse error", d["message"], re.I)
        )
        for d in issues
    ):
        return "wont_build"
    if re.search(
        r"\bmismatch\b|expected.+?(?:actual|got)|\bassertion\b|wrong (?:answer|result)",
        messages,
        re.I,
    ):
        return "wrong_answer"
    # Verilator promotes warnings to a failed exit; its summary is not a new defect.
    substantive = [
        d for d in issues if not re.search(r"Exiting due to \d+ warning", d["message"], re.I)
    ]
    housekeeping = re.compile(
        r"EOFNEWLINE|NEWLINEEOF|newline|timescale|UNUSEDSIGNAL|UNUSEDPARAM|"
        r"unused (?:signal|parameter|wire|input|output)|(?:signal|parameter).+not used",
        re.I,
    )
    housekeeping_codes = {"EOFNEWLINE", "NEWLINEEOF", "TIMESCALEMOD", "UNUSEDSIGNAL", "UNUSEDPARAM"}
    if substantive and all(
        d["severity"] == "warning"
        and (
            d["code"] in housekeeping_codes if d.get("code") else housekeeping.search(d["message"])
        )
        for d in substantive
    ):
        return "housekeeping"
    return "unknown"


def caption_facts(report: RunReport) -> dict:
    failed = next((i for i, a in enumerate(report.attempts) if not a.passed), None)
    diagnostics = []
    change = None
    number = None
    later_passed = False
    kind = "unknown"
    if failed is not None:
        attempt = report.attempts[failed]
        number = attempt.number
        diagnostics = [d.model_dump() for d in attempt.diagnostics]
        diagnostics.extend(d.model_dump() for t in attempt.tools for d in t.diagnostics)
        kind = failure_kind(diagnostics, timed_out=any(t.timed_out for t in attempt.tools))
        later_passed = any(a.passed for a in report.attempts[failed + 1 :])
        if failed + 1 < len(report.attempts):
            change = report.attempts[failed + 1].change_summary
    return {
        "spec": report.spec,
        "diagnostics": diagnostics,
        "change_summary": change,
        "attempt_number": number,
        "total_attempts": len(report.attempts),
        "final_status": report.status,
        "later_attempt_passed": later_passed,
        "failure_kind": kind,
    }


def circuit_name(spec: str) -> str:
    """Everyday names derived only from the spec, never internal module identifiers."""
    for pattern, name in (
        (r"population.?count|popcount|bit.?count", "bit-counter"),
        (r"multiply.{0,20}accumulat|\bMAC\b", "multiply-and-add circuit"),
        (r"register file", "small memory circuit"),
        (r"alpha.?blend|pixel", "pixel blender"),
        (r"\bALU\b|arithmetic.*logic", "arithmetic circuit"),
        (r"\bcounter\b", "counter"),
        (r"\badder\b", "adder"),
    ):
        if re.search(pattern, spec, re.I):
            return name
    return "circuit"


def fallback_caption(facts: dict) -> str:
    name = circuit_name(facts["spec"])
    number = facts["attempt_number"]
    passed = facts["final_status"] == "passed"
    if number is None:
        if passed:
            return f"The AI's first draft of the {name} passed every check. No repair was needed."
        return f"The {name} has no completed checks to explain. No attempt has been shown to pass."
    subject = (
        f"The AI's first draft of the {name}" if number == 1 else f"Attempt {number} at the {name}"
    )
    messages = " ".join(d["message"] for d in facts["diagnostics"])
    pair = re.search(r"expected[=\s]+(-?\d+).*?(?:actual|got)[=\s]+(-?\d+)", messages, re.I)
    kind = facts["failure_kind"]
    if kind == "wrong_answer":
        happened = (
            f"gave {int(pair[2]):,} when the expected answer was {int(pair[1]):,}"
            if pair
            else "gave a wrong answer"
        )
    else:
        happened = {
            "housekeeping": "had a small formatting slip, not a wrong answer",
            "wont_build": "could not be built",
            "timeout": "took too long to finish its checks",
            "unknown": "did not pass every check",
        }.get(kind, "did not pass every check")
    if passed and facts["later_attempt_passed"]:
        total = facts["total_attempts"]
        later = "A second attempt" if total == 2 else f"Attempt {total}"
        outcome = f"{later} fixed it and passed every check."
    elif facts["final_status"] == "failed":
        outcome = "None of the attempts passed every check."
    else:
        outcome = "No later attempt has been shown to pass every check."
    return f"{subject} {happened}. {outcome}"


def start_caption(
    provider: Provider, report: RunReport, save: Callable[[RunReport], None], timeout: float = 10
) -> threading.Thread:
    """Return immediately; only the supervisor writes the eventual caption to disk."""
    facts = caption_facts(report)

    def supervise() -> None:
        result: queue.Queue = queue.Queue(maxsize=1)

        def request() -> None:
            try:
                result.put(provider.caption(facts))
            except Exception:
                result.put(None)

        threading.Thread(target=request, daemon=True, name="rtl-caption-request").start()
        try:
            text = result.get(timeout=timeout)
        except queue.Empty:
            text = None
        if isinstance(text, str) and 0 < len(text.strip()) <= 1600:
            report.caption, report.caption_source = text.strip(), "model"
        report.caption_pending = False
        try:
            save(report)
        except OSError:
            pass  # A failed explanation must not affect verification or raise in a thread.

    thread = threading.Thread(target=supervise, daemon=True, name="rtl-caption")
    thread.start()
    return thread
