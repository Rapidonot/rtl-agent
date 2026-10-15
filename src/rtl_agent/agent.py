"""Generate RTL and a fixed testbench, then repair only the RTL."""

import hashlib
import json
import shutil
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from rtl_agent.models import Attempt, Diagnostic, RunConfig, RunReport
from rtl_agent.policy import validate_source, validate_top
from rtl_agent.providers import Provider, ProviderError
from rtl_agent.tools import EDAToolchain


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _save(report: RunReport) -> None:
    directory = Path(report.run_dir)
    (directory / "report.json").write_text(
        report.model_dump_json(indent=2) + "\n", encoding="utf-8"
    )
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
            "| Attempt | Lint | Compile | Simulation | Outcome |",
            "| --- | --- | --- | --- | --- |",
        ]
    )
    for attempt in report.attempts:
        by_stage = {t.stage: "pass" if t.passed else "fail" for t in attempt.tools}
        values = [by_stage.get(s, "not run") for s in ("lint", "compile", "simulate")]
        lines.append(
            f"| {attempt.number} | {' | '.join(values)} | {'pass' if attempt.passed else 'fail'} |"
        )
    if report.error:
        lines.extend(["", "## Stopped", "", report.error])
    lines.extend(
        [
            "",
            "Passing means the fixed testbench and lint checks passed. Generated tests",
            "are not an independent correctness proof. Synthesis, timing, CDC and formal",
            "verification are outside Stage 1.",
            "",
        ]
    )
    (directory / "report.md").write_text("\n".join(lines), encoding="utf-8")


class RTLAgent:
    def __init__(
        self,
        provider: Provider,
        config: RunConfig | None = None,
        on_event: Callable[[str], None] | None = None,
    ):
        self.provider = provider
        self.config = config or RunConfig()
        validate_top(self.config.top)
        self.toolchain = EDAToolchain(self.config)
        self.emit = on_event or (lambda message: None)

    def run(self, spec: str) -> RunReport:
        if not spec.strip() or len(spec.encode("utf-8")) > 65536:
            raise ValueError("Specification must contain 1–65536 UTF-8 bytes.")
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
        try:
            self.emit("Generating RTL and a self-checking testbench…")
            draft = self.provider.generate(spec, self.config.top)
            (directory / "draft.json").write_text(
                draft.model_dump_json(indent=2) + "\n", encoding="utf-8"
            )
            report.summary = draft.summary
            report.assumptions = draft.assumptions
            tb_problems = validate_source(draft.testbench, top=self.config.top, is_testbench=True)
            if tb_problems:
                raise ValueError("Initial testbench rejected: " + " ".join(tb_problems))
            testbench_bytes = draft.testbench.encode("utf-8")
            (directory / "tb.sv").write_bytes(testbench_bytes)
            testbench_hash = digest(testbench_bytes)
            report.testbench_sha256 = testbench_hash
            self.emit(f"Testbench fixed for this run (SHA-256 {testbench_hash[:12]}…).")
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
                self.emit(f"Attempt {number}/{self.config.max_attempts}: lint, compile, simulate…")
                diagnostics = [
                    Diagnostic(tool="source-policy", severity="error", message=message)
                    for message in policy_problems
                ]
                tools = []
                if not policy_problems:
                    tools = self.toolchain.verify(attempt_dir, rtl_path, tb_copy)
                attempt = Attempt(
                    number=number,
                    rtl_sha256=digest(rtl.encode("utf-8")),
                    testbench_sha256=testbench_hash,
                    change_summary=change_summary,
                    passed=not diagnostics and len(tools) == 3 and all(t.passed for t in tools),
                    tools=tools,
                    diagnostics=diagnostics,
                )
                report.attempts.append(attempt)
                (attempt_dir / "attempt.json").write_text(
                    attempt.model_dump_json(indent=2) + "\n", encoding="utf-8"
                )
                _save(report)
                self.emit(f"Attempt {number}: " + ("PASS" if attempt.passed else "FAIL"))
                if attempt.passed:
                    (directory / f"{self.config.top}.sv").write_text(rtl, encoding="utf-8")
                    if (attempt_dir / "waves.vcd").is_file():
                        shutil.copyfile(attempt_dir / "waves.vcd", directory / "waves.vcd")
                    report.status = "passed"
                    break
                if number < self.config.max_attempts:
                    self.emit("Sending diagnostics to the model for an RTL-only repair…")
                    repair = self.provider.repair(
                        spec, self.config.top, rtl, draft.testbench, attempt
                    )
                    (attempt_dir / "repair.json").write_text(
                        repair.model_dump_json(indent=2) + "\n", encoding="utf-8"
                    )
                    rtl, change_summary = repair.rtl, repair.change_summary
            else:
                report.status = "failed"
                report.error = (
                    "Attempt budget exhausted. Inspect the saved diagnostics and testbench."
                )
        except KeyboardInterrupt:
            report.status = "interrupted"
            report.error = "Interrupted by user. Partial artifacts have been preserved."
        except (ProviderError, OSError, ValueError, RuntimeError) as exc:
            report.status = "error"
            report.error = str(exc)
        finally:
            report.finished_at = utc_now()
            _save(report)
        return report


def load_report(path: Path) -> RunReport:
    if path.is_dir():
        path = path / "report.json"
    return RunReport.model_validate(json.loads(path.read_text(encoding="utf-8")))
