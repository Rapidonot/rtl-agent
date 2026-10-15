"""Run local EDA tools and collect their verification results."""

import os
import re
import shutil
import subprocess
import time
from pathlib import Path

from rtl_agent.models import Diagnostic, RunConfig, ToolResult
from rtl_agent.policy import PASS_MARKER

REQUIRED_TOOLS = ("iverilog", "vvp", "verilator")


class MissingToolError(RuntimeError):
    pass


def tool_paths() -> dict[str, str | None]:
    return {name: shutil.which(name) for name in REQUIRED_TOOLS}


def require_tools() -> dict[str, str]:
    paths = tool_paths()
    missing = [name for name, path in paths.items() if path is None]
    if missing:
        raise MissingToolError(
            f"Missing tools: {', '.join(missing)}. On macOS: brew install icarus-verilog verilator"
        )
    return {name: path for name, path in paths.items() if path is not None}


def parse_diagnostics(output: str, stage: str) -> list[Diagnostic]:
    diagnostics = []
    for line in output.splitlines():
        verilator = re.match(
            r"%(?P<severity>Error|Warning)(?:-(?P<code>[A-Z0-9_]+))?:\s*"
            r"(?P<file>.+?):(?P<line>\d+)(?::\d+)?:\s*(?P<message>.*)",
            line,
        )
        if verilator:
            values = verilator.groupdict()
            diagnostics.append(
                Diagnostic(
                    tool=stage,
                    severity=values["severity"].lower(),
                    message=values["message"],
                    file=values["file"],
                    line=int(values["line"]),
                    code=values["code"],
                )
            )
            continue
        icarus = re.match(r"(?:FATAL:\s*)?(.+?\.s?v):(\d+):\s*(.*)", line)
        if icarus:
            file, number, message = icarus.groups()
            severity = "error"
            if "warning:" in message.lower():
                severity = "warning"
            elif not line.startswith("FATAL:") and message.startswith("$finish called at "):
                severity = "info"
            diagnostics.append(
                Diagnostic(
                    tool=stage,
                    severity=severity,
                    message=message,
                    file=file,
                    line=int(number),
                )
            )
            continue
        if re.match(r"%(Error|Warning)", line) or line.startswith(("ERROR:", "FATAL:")):
            diagnostics.append(
                Diagnostic(
                    tool=stage,
                    severity="warning" if line.startswith("%Warning") else "error",
                    message=line,
                )
            )
    return diagnostics[:100]


def run_command(
    command: list[str], *, stage: str, cwd: Path, timeout: float, max_log_bytes: int
) -> ToolResult:
    start = time.monotonic()
    # EDA subprocesses have no need for model credentials.
    env = {k: v for k, v in os.environ.items() if not k.endswith(("_API_KEY", "_TOKEN"))}
    env["LC_ALL"] = "C"
    timed_out = False
    try:
        result = subprocess.run(
            command,
            cwd=cwd,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            errors="replace",
            timeout=timeout,
        )
        returncode, output = result.returncode, result.stdout
    except subprocess.TimeoutExpired as exc:
        timed_out = True
        returncode = None
        # TimeoutExpired may contain bytes even when text=True.
        output = exc.stdout or ""
        if isinstance(output, bytes):
            output = output.decode("utf-8", errors="replace")
    except OSError as exc:
        returncode = None
        output = f"Could not run {Path(command[0]).name}: {exc}"
    encoded_output = output.encode("utf-8")
    output_limited = len(encoded_output) > max_log_bytes
    output = encoded_output[:max_log_bytes].decode("utf-8", errors="ignore")
    diagnostics = parse_diagnostics(output, stage)
    if timed_out:
        diagnostics.append(
            Diagnostic(
                tool=stage,
                severity="error",
                message=f"Command timed out after {timeout:g} seconds.",
            )
        )
    if output_limited:
        diagnostics.append(
            Diagnostic(
                tool=stage,
                severity="error",
                message=f"Output truncated to {max_log_bytes} bytes.",
            )
        )
    passed = returncode == 0 and not timed_out and not output_limited
    if stage == "simulate":
        # Exit 0 alone (e.g. premature $finish) does not establish a passing test.
        marker_count = output.splitlines().count(PASS_MARKER)
        if marker_count != 1:
            passed = False
            diagnostics.append(
                Diagnostic(
                    tool=stage,
                    severity="error",
                    message=(
                        f"Expected exactly one standalone {PASS_MARKER} line; got {marker_count}."
                    ),
                )
            )
        if any(d.severity == "error" for d in diagnostics):
            passed = False
    if not passed and not diagnostics:
        diagnostics.append(
            Diagnostic(
                tool=stage,
                severity="error",
                message=f"Command failed with exit status {returncode}.",
            )
        )
    return ToolResult(
        stage=stage,
        command=command,
        returncode=returncode,
        duration_seconds=round(time.monotonic() - start, 4),
        output=output,
        passed=passed,
        timed_out=timed_out,
        output_limited=output_limited,
        diagnostics=diagnostics,
    )


class EDAToolchain:
    def __init__(self, config: RunConfig):
        self.config = config
        self.paths = require_tools()

    def verify(self, directory: Path, rtl: Path, testbench: Path) -> list[ToolResult]:
        top = self.config.top
        commands = [
            (
                "lint",
                [
                    self.paths["verilator"],
                    "--lint-only",
                    "-Wall",
                    "-Wno-DECLFILENAME",
                    "--top-module",
                    top,
                    "--language",
                    "1800-2012",
                    rtl.name,
                ],
            ),
            (
                "compile",
                [
                    self.paths["iverilog"],
                    "-g2012",
                    "-Wall",
                    "-s",
                    "tb",
                    "-o",
                    "simulation.vvp",
                    rtl.name,
                    testbench.name,
                ],
            ),
        ]
        results = []
        for stage, command in commands:
            result = run_command(
                command,
                stage=stage,
                cwd=directory,
                timeout=self.config.timeout_seconds,
                max_log_bytes=self.config.max_log_bytes,
            )
            (directory / f"{stage}.log").write_text(result.output, encoding="utf-8")
            results.append(result)
        if results[-1].passed:
            result = run_command(
                [self.paths["vvp"], "-N", "simulation.vvp"],
                stage="simulate",
                cwd=directory,
                timeout=self.config.timeout_seconds,
                max_log_bytes=self.config.max_log_bytes,
            )
            (directory / "simulate.log").write_text(result.output, encoding="utf-8")
            results.append(result)
        return results
