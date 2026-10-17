"""Run local EDA tools and collect their verification results."""

import json
import os
import re
import shutil
import subprocess
import time
from collections import deque
from collections.abc import Callable
from pathlib import Path

from rtl_agent.models import Diagnostic, RunConfig, SynthesisMetrics, ToolResult
from rtl_agent.policy import PASS_MARKER

REQUIRED_TOOLS = ("iverilog", "vvp", "verilator", "yosys")


class MissingToolError(RuntimeError):
    pass


def tool_paths() -> dict[str, str | None]:
    return {name: shutil.which(name) for name in REQUIRED_TOOLS}


def require_tools() -> dict[str, str]:
    paths = tool_paths()
    missing = [name for name, path in paths.items() if path is None]
    if missing:
        raise MissingToolError(
            f"Missing tools: {', '.join(missing)}. "
            "On macOS: brew install icarus-verilog verilator yosys"
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


def parse_synthesis(output: str, top: str) -> SynthesisMetrics:
    """Find stat -json among Yosys log lines and count generic mapped cells."""
    decoder = json.JSONDecoder()
    for start in re.finditer(r"(?m)^\s*\{", output):
        try:
            stats, _ = decoder.raw_decode(output[start.start() :].lstrip())
        except ValueError:
            continue
        modules = stats.get("modules", {})
        module = modules.get(top, modules.get("\\" + top, {}))
        counts = module.get("num_cells_by_type", stats.get("design", {}).get("num_cells_by_type"))
        if not isinstance(counts, dict):
            continue
        flip_flops = sum(n for c, n in counts.items() if c.startswith(("$_DFF", "$_SDFF")))
        latches = sum(n for c, n in counts.items() if c.startswith(("$_DLATCH", "$_SR")))
        depths = [int(n) for n in re.findall(r"length\s*=\s*(\d+)", output)]
        total = sum(counts.values())
        return SynthesisMetrics(
            total_cells=total,
            combinational_gates=total - flip_flops - latches,
            flip_flops=flip_flops,
            latches=latches,
            logic_depth=max(depths) if depths else None,
            cell_breakdown=counts,
        )
    raise ValueError("Yosys output did not contain stat -json cell counts.")


def extract_vcd_trace(vcd_path: Path, max_timestamps: int = 12) -> str:
    """Keep the final timestamp snapshots, carrying forward unchanged signal values."""
    if max_timestamps <= 0:
        return ""
    signals: dict[str, tuple[str, int]] = {}
    scope = []
    snapshots: deque[tuple[str, dict[str, str]]] = deque(maxlen=max_timestamps)
    values: dict[str, str] = {}
    timestamp = None
    try:
        with vcd_path.open(encoding="utf-8", errors="replace") as stream:
            header = []
            for line in stream:
                header.append(line)
                if "$enddefinitions" in line:
                    break
            header_text = "".join(header)
            for entry in re.finditer(r"\$(scope|upscope|var)\b(.*?)\$end", header_text, re.DOTALL):
                kind, definition = entry.groups()
                parts = definition.split()
                if kind == "scope":
                    scope.append(parts[1])
                elif kind == "upscope":
                    scope.pop()
                else:
                    _, width, code, *name = parts
                    # Aliased testbench/DUT ports share a code; display each code once.
                    signals.setdefault(code, (".".join([*scope, "".join(name)]), int(width)))
            tokens = (token for line in stream for token in line.split())
            for token in tokens:
                if token == "$comment":
                    for comment in tokens:
                        if comment == "$end":
                            break
                elif token.startswith("#") and token[1:].isdigit():
                    if timestamp is not None:
                        snapshots.append((timestamp, values.copy()))
                    timestamp = token
                elif token[0] in "bBrR":
                    code = next(tokens, "")
                    if code in signals:
                        bits = token[1:].lower()
                        width = signals[code][1]
                        if token[0].lower() == "b" and set(bits) <= {"0", "1"} and bits:
                            values[code] = f"0x{int(bits, 2):0{(width + 3) // 4}x}"
                        else:
                            values[code] = token.lower()
                elif token[0] in "01xXzZ" and token[1:] in signals:
                    values[token[1:]] = token[0].lower()
            if timestamp is not None:
                snapshots.append((timestamp, values.copy()))
    except (OSError, ValueError, IndexError):
        return ""
    if not signals or not snapshots:
        return ""
    timescale = re.search(r"\$timescale\s+(.*?)\s*\$end", header_text, re.DOTALL)
    unit = "".join(timescale.group(1).split()) if timescale else "VCD ticks"
    columns = [f"Time ({unit})", *(name for name, _ in signals.values())]
    rows = [" | ".join(columns), " | ".join("---" for _ in columns)]
    rows.extend(
        " | ".join([t, *(state.get(code, "?") for code in signals)]) for t, state in snapshots
    )
    return "\n".join(rows)


class EDAToolchain:
    def __init__(self, config: RunConfig):
        self.config = config
        self.paths = require_tools()
        self.on_result: Callable[[ToolResult, SynthesisMetrics | None], None] | None = None
        self.command_timeout: Callable[[], float] = lambda: self.config.timeout_seconds

    def verify(
        self, directory: Path, rtl: Path, testbench: Path
    ) -> tuple[list[ToolResult], SynthesisMetrics | None]:
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
        synthesis = None
        for stage, command in commands:
            result = run_command(
                command,
                stage=stage,
                cwd=directory,
                timeout=self.command_timeout(),
                max_log_bytes=self.config.max_log_bytes,
            )
            (directory / f"{stage}.log").write_text(result.output, encoding="utf-8")
            results.append(result)
            if self.on_result:
                self.on_result(result, synthesis)
        if results[-1].passed:
            result = run_command(
                [self.paths["vvp"], "-N", "simulation.vvp"],
                stage="simulate",
                cwd=directory,
                timeout=self.command_timeout(),
                max_log_bytes=self.config.max_log_bytes,
            )
            (directory / "simulate.log").write_text(result.output, encoding="utf-8")
            results.append(result)
            if self.on_result:
                self.on_result(result, synthesis)
        if results[-1].stage == "simulate" and results[-1].passed:
            # Keep stdout: -q suppresses stat/ltp. Flatten and stop depth paths at FFs.
            script = (
                f"read_verilog -sv {json.dumps(rtl.name)}; synth -top {top} -flatten; "
                "delete t:$scopeinfo; check -assert; ltp -noff; stat -json; "
                "write_verilog -noattr netlist.v"
            )
            result = run_command(
                [self.paths["yosys"], "-Q", "-T", "-p", script],
                stage="synth",
                cwd=directory,
                timeout=self.command_timeout(),
                max_log_bytes=self.config.max_log_bytes,
            )
            if result.passed:
                try:
                    synthesis = parse_synthesis(result.output, top)
                    if synthesis.latches:
                        raise ValueError(
                            f"Inferred {synthesis.latches} accidental level-sensitive latches; "
                            "replace them with proper combinational defaults or clocked registers."
                        )
                except ValueError as exc:
                    result.passed = False
                    result.diagnostics.append(
                        Diagnostic(tool="synth", severity="error", message=str(exc))
                    )
            (directory / "synth.log").write_text(result.output, encoding="utf-8")
            results.append(result)
            if self.on_result:
                self.on_result(result, synthesis)
        return results, synthesis
