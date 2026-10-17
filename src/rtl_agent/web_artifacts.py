"""Read-only, bounded adapters for real run artifacts and recorded VCD samples."""

import json
import re
from datetime import datetime
from pathlib import Path

from rtl_agent.models import Attempt, RunReport
from rtl_agent.policy import PASS_MARKER

RUN_ID = re.compile(r"\d{8}T\d{6}Z-[0-9a-f]{8}")
UNITS = {"s": 1e9, "ms": 1e6, "us": 1e3, "ns": 1, "ps": 1e-3, "fs": 1e-6}
EXAMPLES = {
    "cpu_alu8": ("CPU", "8-bit ALU"),
    "cpu_reg_file": ("CPU", "Register file"),
    "gpu_simd_alu": ("GPU", "SIMD ALU"),
    "gpu_alpha_blend": ("GPU", "Alpha blender"),
    "npu_neural_mac": ("NPU", "INT8 multiply–accumulate"),
}


def gallery_description(spec: str) -> str:
    """Describe the requested block, without repeating an unreviewed model summary."""
    for pattern, description in (
        (r"population.?count|popcount|bit.?count", "Counts the 1 bits in each input value."),
        (
            r"multiply.{0,40}accumulat|\bMAC\b",
            "Multiplies two inputs and adds each result to a running total.",
        ),
        (r"register file", "Stores values in a small register bank for reading and updating."),
        (
            r"alpha.?blend|pixel.*blend",
            "Combines foreground and background pixels using an alpha value.",
        ),
        (r"\bSIMD\b", "Applies the same operation across several data lanes at once."),
        (
            r"\bALU\b|arithmetic.*logic",
            "Performs the arithmetic and logic operations in your specification.",
        ),
        (r"\bcounter\b", "Counts events according to the rules in your specification."),
        (r"\badder\b", "Adds input values and produces their sum."),
    ):
        if re.search(pattern, spec, re.I):
            return description
    return (
        "A custom circuit built from your specification. "
        "Open the result to inspect its recorded checks."
    )


def artifact(root: Path, run_id: str, *parts: str) -> Path:
    if not RUN_ID.fullmatch(run_id):
        raise ValueError("Invalid run ID.")
    root = root.resolve()
    base = (root / run_id).resolve()
    path = base.joinpath(*parts).resolve()
    if not base.is_relative_to(root) or not path.is_relative_to(base):
        raise ValueError("Artifact path is outside its run directory.")
    return path


def read_text(path: Path, limit: int = 2 * 1024 * 1024) -> str | None:
    try:
        if path.stat().st_size > limit:
            return None
        return path.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return None


def vcd_header(path: Path) -> tuple[str, float | None, str | None]:
    try:
        with path.open(encoding="utf-8", errors="replace") as stream:
            text = stream.read(131072)
    except FileNotFoundError:
        return "", None, None
    if "$enddefinitions" not in text:
        return "", None, None
    text = text.split("$enddefinitions", 1)[0]
    match = re.search(r"\$timescale\s+(\d+(?:\.\d+)?)\s*(fs|ps|ns|us|ms|s)\s*\$end", text)
    if not match:
        return text, None, None
    return text, float(match[1]) * UNITS[match[2]], "".join(match.groups())


def waveform(path: Path) -> dict | None:
    return waveform_with_reason(path)[0]


def waveform_with_reason(path: Path) -> tuple[dict | None, str | None]:
    """Return only recorded samples, or a precise display-only absence reason."""
    if not path.is_file():
        return None, "No waves.vcd file exists for this attempt."
    if path.stat().st_size == 0:
        return None, "The waves.vcd file is empty."
    if path.stat().st_size > 32 * 1024 * 1024:
        raise ValueError("Waveform exceeds the 32 MiB display limit; inspect the local VCD file.")
    header, factor, unit = vcd_header(path)
    if not header:
        return None, "The VCD header is incomplete or exceeds the 128 KiB display limit."
    if factor is None:
        return None, "The VCD has no supported $timescale entry; its time unit is unknown."
    signals = {}
    scope = []
    partial = False
    for match in re.finditer(r"\$(scope|upscope|var)\b(.*?)\$end", header, re.S):
        kind, definition = match.groups()
        parts = definition.split()
        if kind == "scope" and len(parts) >= 2:
            scope.append(parts[1])
        elif kind == "upscope" and scope:
            scope.pop()
        elif kind == "var" and len(parts) >= 4:
            _, width, code, *name = parts
            if code in signals:
                continue  # Port aliases share exactly the same recorded signal.
            if len(signals) >= 64 or not width.isdigit() or not 1 <= int(width) <= 4096:
                partial = True
                continue
            signals[code] = {
                "name": ".".join([*scope, "".join(name)]),
                "width": int(width),
                "format": "hex",
                "samples": [],
            }
    start = end = None
    count = 0
    with path.open(encoding="utf-8", errors="replace") as stream:
        for line in stream:
            if "$enddefinitions" in line:
                break
        tokens = iter(token for line in stream for token in line.split())
        time = None
        for token in tokens:
            if token.startswith("#") and token[1:].isdigit():
                time = int(token[1:]) * factor
                if end is not None and time < end:
                    return None, "VCD timestamps move backwards; the trace cannot be plotted."
                start = time if start is None else start
                end = time
                continue
            if token.startswith("$"):
                if token in ("$comment", "$date", "$version"):
                    for comment in tokens:
                        if comment == "$end":
                            break
                continue
            if time is None:
                continue
            if token[:1].lower() in ("b", "r"):
                code, value = next(tokens, ""), token.lower()
                channel = signals.get(code)
                if not channel:
                    continue
                bits = value[1:]
                if value[0] == "b" and bits and set(bits) <= {"0", "1"}:
                    if channel["width"] <= 32:
                        value = int(bits, 2)
                elif not re.fullmatch(r"b[01xz]+|r[-+\d.e]+", value):
                    continue
            else:
                value, code = token[:1].lower(), token[1:]
                channel = signals.get(code)
                if not channel or value not in "01xz":
                    continue
                value = int(value) if value in "01" and channel["width"] <= 32 else value
            samples = channel["samples"]
            if not samples or samples[-1][1] != value:
                samples.append([time, value])
                count += 1
                if count >= 100000:
                    partial = True
                    break
    channels = [c for c in signals.values() if c["samples"]]
    if start is None or end is None:
        return None, "The VCD contains no recorded timestamps."
    if not channels:
        return None, "The VCD contains no supported signal samples."
    if end <= start:
        return None, "The VCD records only one time instant; there is no time span to plot."
    return {
        "start_ns": start,
        "duration_ns": end,
        "channels": channels,
        "partial": partial,
        "timescale": unit,
        "provenance": "waves.vcd",
    }, None


def diagnostic(attempt: Attempt, wave_path: Path) -> dict:
    failed = [t for t in attempt.tools if not t.passed]
    preferred = next((t for t in failed if t.stage == "simulate"), None)
    preferred = preferred or next(iter(failed), None)
    diagnostics = preferred.diagnostics if preferred else attempt.diagnostics
    issue = next((d for d in diagnostics if d.severity == "error"), None)
    if preferred is None and issue is None:
        return {"diagnostic": None, "raw_error": ""}
    raw = preferred.output if preferred else issue.message
    time = None
    if preferred and preferred.stage == "simulate":
        match = re.search(r"\bTime:\s*(\d+(?:\.\d+)?)\s*(ns|ps|us|ms|s)?", raw)
        if match:
            factor = UNITS[match[2]] if match[2] else vcd_header(wave_path)[1]
            if factor is not None:
                time = float(match[1]) * factor
    return {
        "diagnostic": {
            "message": issue.message if issue else "Verification failed.",
            "time_ns": time,
            "stage": preferred.stage if preferred else issue.tool,
        },
        "raw_error": raw,
    }


def ui_report(
    report: RunReport, root: Path, max_attempts: int, known_example: bool = False
) -> dict:
    started = datetime.fromisoformat(report.started_at)
    ended = datetime.fromisoformat(report.finished_at) if report.finished_at else started
    category, title = EXAMPLES[report.top] if known_example else ("Custom", "Custom chip block")
    result = {
        "ui_schema_version": 2,
        "run_id": report.run_id,
        "source": "live",
        "model": report.model,
        "spec": report.spec,
        "description": gallery_description(report.spec),
        "top": report.top,
        "title": title,
        "category": category,
        "status": report.status,
        "elapsed_ms": max(0, (ended - started).total_seconds() * 1000),
        "max_attempts": max_attempts,
        "testbench_sha256": report.testbench_sha256,
        "assumptions": report.assumptions,
        "error": report.error,
        "caption": report.caption,
        "caption_source": report.caption_source,
        "caption_pending": report.caption_pending,
        "attempts": [],
    }
    attempts = [*report.attempts]
    if report.current_attempt:
        attempts.append(report.current_attempt)
    for attempt in attempts:
        active = attempt is report.current_attempt
        directory = ("attempts", f"{attempt.number:02d}")
        wave_path = artifact(root, report.run_id, *directory, "waves.vcd")
        gates = dict.fromkeys(("lint", "compile", "simulate", "synth", "marker"), "off")
        for tool in attempt.tools:
            gates[tool.stage] = "pass" if tool.passed else "fail"
            if tool.stage == "simulate":
                gates["marker"] = (
                    "pass"
                    if (tool.passed and tool.output.splitlines().count(PASS_MARKER) == 1)
                    else "fail"
                )
        status = (
            "running"
            if active and report.status == "running"
            else ("interrupted" if active else "passed" if attempt.passed else "failed")
        )
        synthesis = attempt.synthesis.model_dump() if attempt.synthesis else None
        if synthesis:
            tool = next((t for t in attempt.tools if t.stage == "synth"), None)
            if tool:
                for match in re.finditer(r"(?m)^\s*\{", tool.output):
                    try:
                        data, _ = json.JSONDecoder().raw_decode(
                            tool.output[match.start() :].lstrip()
                        )
                        module = data.get("modules", {}).get("\\" + report.top, {})
                        module = module or data.get("modules", {}).get(report.top, {})
                        for key in ("num_wires", "num_wire_bits"):
                            if isinstance(module.get(key), int):
                                synthesis[key] = module[key]
                    except ValueError:
                        continue
        result["attempts"].append(
            {
                "number": attempt.number,
                "status": status,
                "gates": gates,
                "rtl_sha256": attempt.rtl_sha256,
                "testbench_sha256": attempt.testbench_sha256,
                "rtl": read_text(artifact(root, report.run_id, *directory, report.top + ".sv")),
                "testbench": read_text(artifact(root, report.run_id, *directory, "tb.sv")),
                "tools": [t.model_dump() for t in attempt.tools],
                "synthesis": synthesis,
                "change_summary": attempt.change_summary,
                "elapsed_ms": sum(t.duration_seconds for t in attempt.tools) * 1000,
                "log": "\n".join(
                    f"[{t.stage.upper()}] {'PASS' if t.passed else 'FAIL'}\n{t.output}"
                    for t in attempt.tools
                ),
                "waveform": None,
                "waveform_available": wave_path.is_file(),
                "waveform_reason": (
                    None if wave_path.is_file() else "No waves.vcd file exists for this attempt."
                ),
                "cycles": None,
                **diagnostic(attempt, wave_path),
            }
        )
    return result
