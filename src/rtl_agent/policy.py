"""Essential checks for RTL source and the self-checking testbench protocol."""

import re

PASS_MARKER = "RTL_AGENT_PASS"
TB_TOP = "tb"
_LEXEMES = re.compile(r'//[^\n]*|/\*[\s\S]*?\*/|"(?:\\.|[^"\\])*"')
_MODULE = re.compile(r"\bmodule\s+(?:(?:automatic|static)\s+)?([A-Za-z_][A-Za-z0-9_]*)")


def validate_top(top: str) -> None:
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", top) or top == TB_TOP:
        raise ValueError("Top must be a simple Verilog identifier other than 'tb'.")


def validate_source(source: str, *, top: str, is_testbench: bool = False) -> list[str]:
    problems = []
    label = "Testbench" if is_testbench else "RTL"
    if "```" in source:
        problems.append(f"{label} must be raw code, without Markdown fences.")
    # Ignore comments and strings when looking for Verilog keywords and tasks.
    code = _LEXEMES.sub(lambda m: " " * len(m[0]), source)
    modules = _MODULE.findall(code)
    expected = TB_TOP if is_testbench else top
    if modules.count(expected) != 1:
        problems.append(f"{label} must declare exactly one module named {expected}.")
    if not is_testbench:
        if TB_TOP in modules:
            problems.append("RTL must not define the testbench module.")
        if re.search(r"\binitial\b", code):
            problems.append("RTL cannot contain initial blocks.")
        if re.search(r"verilator\s+lint_(?:off|on)", source, re.I):
            problems.append("RTL cannot suppress lint warnings.")
        if re.search(r"\$(?:display|write|finish|stop|fatal|error|system)\b", code):
            problems.append("RTL cannot print results or control the simulation.")
    else:
        # Keep string literals for the exact success-marker and waveform checks.
        uncommented = _LEXEMES.sub(
            lambda m: m[0] if m[0].startswith('"') else " " * len(m[0]), source
        )
        checks = [
            (r"\$fatal\s*\(", code, "self-checking $fatal assertions"),
            (
                r'\$display\s*\(\s*"' + PASS_MARKER + r'"\s*\)',
                uncommented,
                f'$display("{PASS_MARKER}")',
            ),
            (r"\$finish\s*(?:\(|;)", code, "$finish"),
            (r'\$dumpfile\s*\(\s*"waves\.vcd"\s*\)', uncommented, '$dumpfile("waves.vcd")'),
        ]
        for pattern, text, required in checks:
            if not re.search(pattern, text):
                problems.append(f"Testbench must contain {required}.")
    return problems
