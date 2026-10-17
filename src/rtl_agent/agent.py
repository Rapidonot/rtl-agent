"""Generate RTL and a fixed testbench, then repair only the RTL."""

import hashlib
import json
import os
import re
import shutil
import tempfile
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from rtl_agent.captions import caption_facts, fallback_caption, start_caption
from rtl_agent.control import RunControl, RunStopped
from rtl_agent.models import Attempt, Diagnostic, RunConfig, RunReport
from rtl_agent.policy import validate_source, validate_top
from rtl_agent.providers import Provider, ProviderError
from rtl_agent.tools import EDAToolchain, extract_vcd_trace


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def ensure_testbench_timescale(source: str) -> str:
    # Comments and quoted text cannot supply an active timing directive.
    code = re.sub(r'//[^\n]*|/\*[\s\S]*?\*/|"(?:\\.|[^"\\])*"', " ", source)
    if re.search(r"`timescale\b", code):
        return source
    return "`timescale 1ns/1ps\n" + source


def _atomic_text(path: Path, text: str) -> None:
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=path.parent, delete=False
    ) as stream:
        temporary = Path(stream.name)
        try:
            stream.write(text)
            stream.flush()
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)


def _save(report: RunReport) -> None:
    directory = Path(report.run_dir)
    _atomic_text(directory / "report.json", report.model_dump_json(indent=2) + "\n")
    lines = [
        "# RTL Agent run",
        "",
        f"Status: **{report.status.upper()}**",
        "",
        f"- Top module: `{report.top}`",
        f"- Provider: `{report.provider}` / `{report.model}`",
        f"- Attempts: {len(report.attempts)}",
        f"- Testbench SHA-256: `{report.testbench_sha256 or 'not generated'}`",
        "",
        "## Specification",
        "",
        report.spec,
        "",
        "## Design summary",
        "",
        report.summary or "No design returned.",
        "",
    ]
    if report.assumptions:
        lines.extend(["## Assumptions", ""] + [f"- {a}" for a in report.assumptions] + [""])
    lines.extend(
        [
            "## Attempts",
            "",
            "| Attempt | Lint | Compile | Simulation | Synthesis | Gates | DFFs | Depth | Result |",
            "| --- | --- | --- | --- | --- | --- | --- | --- | --- |",
        ]
    )
    for attempt in report.attempts:
        by_stage = {t.stage: "pass" if t.passed else "fail" for t in attempt.tools}
        values = [by_stage.get(s, "not run") for s in ("lint", "compile", "simulate", "synth")]
        metrics = attempt.synthesis
        values.extend(
            str(n) if n is not None else "—"
            for n in (
                metrics.combinational_gates if metrics else None,
                metrics.flip_flops if metrics else None,
                metrics.logic_depth if metrics else None,
            )
        )
        lines.append(
            f"| {attempt.number} | {' | '.join(values)} | {'pass' if attempt.passed else 'fail'} |"
        )
    if report.error:
        lines.extend(["", "## Stopped", "", report.error])
    if report.caption:
        lines.extend(["", f"## Explanation ({report.caption_source})", "", report.caption])
    lines.extend(
        [
            "",
            "Passing means lint, the fixed testbench and latch-free synthesis passed.",
            "Gate counts are an area proxy, not physical silicon area. Depth is a gate-level",
            "path length, not a timing estimate. Passing is not a formal correctness proof.",
            "",
        ]
    )
    _atomic_text(directory / "report.md", "\n".join(lines))


class RTLAgent:
    def __init__(
        self,
        provider: Provider,
        config: RunConfig | None = None,
        on_event: Callable[[str], None] | None = None,
        control: RunControl | None = None,
        captions: bool = False,
    ):
        self.provider = provider
        self.config = config or RunConfig()
        validate_top(self.config.top)
        self.toolchain = EDAToolchain(self.config)
        self.emit = on_event or (lambda message: None)
        self.control = control
        self.captions = captions
        if control is not None:
            self.toolchain.command_timeout = lambda: control.remaining(self.config.timeout_seconds)

    def run(
        self,
        spec: str,
        testbench: str | None = None,
        *,
        on_created: Callable[[RunReport], None] | None = None,
    ) -> RunReport:
        if not spec.strip() or len(spec.encode("utf-8")) > 65536:
            raise ValueError("Specification must contain 1–65536 UTF-8 bytes.")
        if testbench is not None:
            problems = validate_source(testbench, top=self.config.top, is_testbench=True)
            if problems:
                raise ValueError("Supplied testbench rejected: " + " ".join(problems))
        run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid4().hex[:8]
        directory = self.config.output_dir.expanduser().resolve() / run_id
        directory.mkdir(parents=True, exist_ok=False)
        report = RunReport(
            run_id=run_id,
            provider=self.provider.name,
            model=self.provider.model,
            spec=spec.strip(),
            top=self.config.top,
            started_at=utc_now(),
            run_dir=str(directory),
        )
        (directory / "spec.txt").write_text(spec.strip() + "\n", encoding="utf-8")
        (directory / "config.json").write_text(
            self.config.model_dump_json(indent=2) + "\n", encoding="utf-8"
        )
        _save(report)
        if on_created:
            on_created(report)
        try:
            if self.control:
                self.control.check()
            if testbench is None:
                self.emit("Generating a self-checking testbench from the specification…")
                reference = self.provider.generate_testbench(spec, self.config.top)
                testbench = ensure_testbench_timescale(reference.testbench)
                reference = reference.model_copy(update={"testbench": testbench})
                report.assumptions = list(dict.fromkeys(reference.assumptions))
                (directory / "testbench.json").write_text(
                    reference.model_dump_json(indent=2) + "\n", encoding="utf-8"
                )
            else:
                testbench = ensure_testbench_timescale(testbench)
            if self.control:
                self.control.check()
            tb_problems = validate_source(testbench, top=self.config.top, is_testbench=True)
            if tb_problems:
                raise ValueError("Initial testbench rejected: " + " ".join(tb_problems))
            testbench_bytes = testbench.encode("utf-8")
            (directory / "tb.sv").write_bytes(testbench_bytes)
            testbench_hash = digest(testbench_bytes)
            report.testbench_sha256 = testbench_hash
            self.emit(f"Testbench fixed for this run (SHA-256 {testbench_hash[:12]}…).")
            _save(report)
            self.emit("Generating RTL for the fixed testbench…")
            draft = self.provider.generate(spec, self.config.top, testbench)
            draft.testbench = testbench
            if self.control:
                self.control.check()
            (directory / "draft.json").write_text(
                draft.model_dump_json(indent=2) + "\n", encoding="utf-8"
            )
            report.summary = draft.summary
            report.assumptions = list(dict.fromkeys([*report.assumptions, *draft.assumptions]))
            rtl = draft.rtl
            change_summary = "Initial generation."
            for number in range(1, self.config.max_attempts + 1):
                attempt_dir = directory / "attempts" / f"{number:02d}"
                attempt_dir.mkdir(parents=True)
                rtl_path = attempt_dir / f"{self.config.top}.sv"
                rtl_path.write_text(rtl, encoding="utf-8")
                tb_copy = attempt_dir / "tb.sv"
                tb_copy.write_bytes(testbench_bytes)
                policy_problems = validate_source(rtl, top=self.config.top)
                self.emit(
                    f"Attempt {number}/{self.config.max_attempts}: "
                    "lint, compile, simulate, synthesize…"
                )
                diagnostics = [
                    Diagnostic(tool="source-policy", severity="error", message=message)
                    for message in policy_problems
                ]
                tools = []
                synthesis = None
                waveform_trace = None
                progress_tools = []

                def progress(
                    result,
                    metrics,
                    *,
                    progress_tools=progress_tools,
                    number=number,
                    rtl=rtl,
                    change_summary=change_summary,
                ):
                    progress_tools.append(result)
                    report.current_attempt = Attempt(
                        number=number,
                        rtl_sha256=digest(rtl.encode("utf-8")),
                        testbench_sha256=testbench_hash,
                        change_summary=change_summary,
                        passed=False,
                        tools=list(progress_tools),
                        synthesis=metrics,
                    )
                    _save(report)
                    if self.control:
                        self.control.check()

                report.current_attempt = Attempt(
                    number=number,
                    rtl_sha256=digest(rtl.encode("utf-8")),
                    testbench_sha256=testbench_hash,
                    change_summary=change_summary,
                    passed=False,
                    tools=[],
                    diagnostics=diagnostics,
                )
                _save(report)
                self.toolchain.on_result = progress
                if not policy_problems:
                    tools, synthesis = self.toolchain.verify(attempt_dir, rtl_path, tb_copy)
                    if any(t.stage == "simulate" and not t.passed for t in tools):
                        waveform_trace = extract_vcd_trace(attempt_dir / "waves.vcd") or None
                        if waveform_trace:
                            (attempt_dir / "waveform_trace.txt").write_text(
                                waveform_trace + "\n", encoding="utf-8"
                            )
                attempt = Attempt(
                    number=number,
                    rtl_sha256=digest(rtl.encode("utf-8")),
                    testbench_sha256=testbench_hash,
                    change_summary=change_summary,
                    passed=not diagnostics and len(tools) == 4 and all(t.passed for t in tools),
                    tools=tools,
                    diagnostics=diagnostics,
                    synthesis=synthesis,
                    waveform_trace=waveform_trace,
                )
                report.attempts.append(attempt)
                report.current_attempt = None
                (attempt_dir / "attempt.json").write_text(
                    attempt.model_dump_json(indent=2) + "\n", encoding="utf-8"
                )
                _save(report)
                self.emit(f"Attempt {number}: " + ("PASS" if attempt.passed else "FAIL"))
                if attempt.passed:
                    (directory / f"{self.config.top}.sv").write_text(rtl, encoding="utf-8")
                    if (attempt_dir / "waves.vcd").is_file():
                        shutil.copyfile(attempt_dir / "waves.vcd", directory / "waves.vcd")
                    shutil.copyfile(attempt_dir / "netlist.v", directory / "netlist.v")
                    report.status = "passed"
                    report.synthesis = synthesis
                    break
                if number < self.config.max_attempts:
                    self.emit("Sending diagnostics to the model for an RTL-only repair…")
                    if self.control:
                        self.control.check()
                    repair = self.provider.repair(
                        spec, self.config.top, rtl, draft.testbench, attempt
                    )
                    if self.control:
                        self.control.check()
                    (attempt_dir / "repair.json").write_text(
                        repair.model_dump_json(indent=2) + "\n", encoding="utf-8"
                    )
                    rtl, change_summary = repair.rtl, repair.change_summary
            else:
                report.status = "failed"
                report.error = (
                    "Attempt budget exhausted. Inspect the saved diagnostics and testbench."
                )
        except RunStopped as exc:
            report.status = "interrupted"
            report.error = str(exc)
        except KeyboardInterrupt:
            report.status = "interrupted"
            report.error = "Interrupted by user. Partial artifacts have been preserved."
        except (ProviderError, OSError, ValueError, RuntimeError) as exc:
            report.status = "error"
            message = str(exc)
            key = os.environ.get("OPENAI_API_KEY", "")
            report.error = message.replace(key, "[redacted]") if key else message
        finally:
            self.toolchain.on_result = None
            if self.control:
                try:
                    self.control.check()
                except RunStopped as exc:
                    report.status, report.error = "interrupted", str(exc)
            report.finished_at = utc_now()
            if self.captions and report.status in ("passed", "failed"):
                report.caption = fallback_caption(caption_facts(report))
                report.caption_source = "deterministic"
                report.caption_pending = True
            _save(report)
        if self.captions and report.status in ("passed", "failed"):
            start_caption(self.provider, report, _save)
        return report


def load_report(path: Path) -> RunReport:
    if path.is_dir():
        path = path / "report.json"
    return RunReport.model_validate(json.loads(path.read_text(encoding="utf-8")))
