# Architecture

The code follows one loop: **specification → generation → verification → RTL repair**.

| Module | Responsibility |
| --- | --- |
| `cli.py` | `generate`, `doctor`, `inspect`, and terminal output |
| `agent.py` | Run folders, fixed testbench bytes, attempt budget and reports |
| `providers.py` | OpenAI Structured Outputs for generation and RTL-only repair |
| `models.py` | Pydantic schemas for model responses and run results |
| `tools.py` | Tool discovery, `subprocess.run`, diagnostics and pass/fail decisions |
| `policy.py` | Essential RTL and testbench format/protocol checks |
| `prompts.py` | Generation and repair instructions |

The model returns text in memory. Host code chooses filenames and command arguments,
writes files and runs tools. A repair response contains `rtl` and `change_summary`;
it cannot contain a replacement testbench. The original testbench bytes are reused
for every attempt and their SHA-256 digest is recorded once. No filesystem locks
or repeated integrity checks are needed for that separation.

Lint and compilation run for each valid RTL candidate. Compilation failure skips
simulation; lint failure still allows compilation and simulation to collect useful
evidence. Success requires all tools to pass, exactly one testbench success marker,
and no simulation assertion errors. An invalid testbench stops the run because
repairs are restricted to RTL. API errors and interrupts preserve completed work.

Tool commands use argument lists without shell interpolation. `subprocess.run`
captures combined stdout/stderr and handles the direct child's timeout. The host
then truncates saved output to the configured byte cap and marks truncation as a
failed result. It does not enforce process-tree isolation, memory limits, or VCD
size limits. Model credentials are removed from EDA subprocess environments.

Tests use a signed NPU MAC and reference testbench under `tests/fixtures/`. Python
mocks replace only the provider's response boundary; Icarus, Verilator and `vvp`
actually run. Separate HTTP-mocked tests exercise the real OpenAI SDK and schemas.

The five specification examples focus on CPU ALUs/register files, GPU SIMD/pixel
pipelines and NPU MACs. Yosys and independent reference testbenches can extend the
same tool/result loop later, without adding another orchestration framework.
