"""Generate hardware, check the environment, and inspect saved runs."""

import os
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table
from rich.text import Text

from rtl_agent.agent import RTLAgent, load_report
from rtl_agent.models import RunConfig, RunReport
from rtl_agent.providers import OpenAIProvider, ProviderError
from rtl_agent.tools import MissingToolError, run_command, tool_paths

app = typer.Typer(
    no_args_is_help=True, help="Generate and verify CPU, GPU, and NPU building blocks."
)
console = Console()


def show_report(report: RunReport) -> None:
    table = Table(title=f"RTL Agent · {report.status.upper()}", padding=(0, 0))
    for heading in (
        "Attempt",
        "Lint",
        "Compile",
        "Simulation",
        "Synthesis",
        "Gates",
        "DFFs",
        "Depth",
        "Result",
    ):
        table.add_column(heading)
    for attempt in report.attempts:
        by_stage = {t.stage: "pass" if t.passed else "fail" for t in attempt.tools}
        metrics = attempt.synthesis
        table.add_row(
            str(attempt.number),
            *(by_stage.get(s, "—") for s in ("lint", "compile", "simulate", "synth")),
            str(metrics.combinational_gates) if metrics else "—",
            str(metrics.flip_flops) if metrics else "—",
            str(metrics.logic_depth) if metrics and metrics.logic_depth is not None else "—",
            Text("PASS" if attempt.passed else "FAIL", style="green" if attempt.passed else "red"),
        )
    console.print(table)
    if report.error:
        console.print(Text(report.error, style="red"))
    console.print(Text(f"Artifacts: {report.run_dir}"))
    if report.passed:
        console.print("Passed lint, the fixed testbench and synthesis; this is not a formal proof.")


def _run(agent: RTLAgent, spec: str, testbench: str | None = None) -> None:
    report = agent.run(spec, testbench)
    show_report(report)
    code = {"passed": 0, "failed": 1, "error": 2, "interrupted": 130}.get(report.status, 2)
    raise typer.Exit(code)


@app.command()
def doctor() -> None:
    """Check EDA binaries and OpenAI configuration without making an API call."""
    table = Table(title="RTL Agent · environment")
    table.add_column("Tool")
    table.add_column("Status / version")
    paths = tool_paths()
    for tool, path in paths.items():
        if path is None:
            table.add_row(tool, Text("missing", style="red"))
            continue
        result = run_command(
            [path, "--version" if tool == "verilator" else "-V"],
            stage="version",
            cwd=Path.cwd(),
            timeout=5,
            max_log_bytes=32768,
        )
        version = next((line for line in result.output.splitlines() if line.strip()), path)
        table.add_row(tool, Text(version))
    table.add_row("OPENAI_API_KEY", "set" if os.environ.get("OPENAI_API_KEY") else "not set")
    table.add_row(
        "RTL_AGENT_MODEL", Text(os.environ.get("RTL_AGENT_MODEL") or "not set; use --model")
    )
    console.print(table)
    if any(path is None for path in paths.values()):
        console.print("macOS: brew install icarus-verilog verilator yosys")
        raise typer.Exit(2)


@app.command()
def generate(
    spec: Annotated[
        str | None, typer.Argument(help="Natural-language circuit specification.")
    ] = None,
    spec_file: Annotated[
        Path | None, typer.Option("--spec-file", help="Read the spec from a UTF-8 file.")
    ] = None,
    tb_file: Annotated[
        Path | None,
        typer.Option("--tb-file", help="Use a fixed, self-checking SystemVerilog testbench."),
    ] = None,
    model: Annotated[
        str | None, typer.Option(envvar="RTL_AGENT_MODEL", help="OpenAI API model ID.")
    ] = None,
    top: Annotated[str, typer.Option(help="RTL top module name; testbench is always tb.")] = "dut",
    max_attempts: Annotated[
        int, typer.Option(min=1, max=20, help="Includes the first candidate.")
    ] = 3,
    timeout: Annotated[float, typer.Option(min=0.1, max=600, help="Seconds per EDA command.")] = 10,
    api_timeout: Annotated[
        float, typer.Option(min=1, max=600, help="Seconds per API request.")
    ] = 120,
    max_output_tokens: Annotated[int, typer.Option(min=1024, max=65536)] = 12000,
    output: Annotated[Path, typer.Option(help="Parent folder for isolated run artifacts.")] = Path(
        "runs"
    ),
) -> None:
    """Generate a fixed testbench, then RTL; repair only RTL until checks pass."""
    if (spec is None) == (spec_file is None):
        raise typer.BadParameter("Provide either SPEC or --spec-file, exactly one.")
    if model is None:
        raise typer.BadParameter("Set --model or RTL_AGENT_MODEL to your OpenAI model ID.")
    try:
        specification = spec if spec is not None else spec_file.read_text(encoding="utf-8")
        testbench = tb_file.read_text(encoding="utf-8") if tb_file is not None else None
        config = RunConfig(
            top=top,
            max_attempts=max_attempts,
            timeout_seconds=timeout,
            output_dir=output,
        )
        provider = OpenAIProvider(
            model,
            timeout_seconds=api_timeout,
            max_output_tokens=max_output_tokens,
        )
        agent = RTLAgent(provider, config, on_event=lambda text: console.print(Text(text)))
        _run(agent, specification, testbench)
    except (ValueError, OSError, MissingToolError, ProviderError) as exc:
        console.print(Text(str(exc), style="red"))
        raise typer.Exit(2) from exc


@app.command()
def inspect(path: Annotated[Path, typer.Argument(help="Run folder or report.json path.")]) -> None:
    """Display a saved run without contacting OpenAI or rerunning tools."""
    try:
        show_report(load_report(path))
    except (OSError, ValueError) as exc:
        console.print(Text(str(exc), style="red"))
        raise typer.Exit(2) from exc
