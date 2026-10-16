import json

import pytest

from rtl_agent.models import RunConfig, ToolResult
from rtl_agent.tools import EDAToolchain, parse_synthesis

CELLS = {
    "$_AND_": 3,
    "$_MUX_": 2,
    "$_DFF_P_": 4,
    "$_SDFF_PP0_": 5,
    "$_DFFE_PP_": 6,
    "$_SDFFE_PP0P_": 7,
    "$_DLATCH_P_": 8,
    "$_SR_PP_": 9,
}


@pytest.mark.parametrize("location", ["dut", "\\dut", "design"])
def test_parse_yosys_cells_and_longest_path(location):
    module = {"num_cells_by_type": CELLS}
    stats = {"design": module} if location == "design" else {"modules": {location: module}}
    output = "Synthesis log\nLongest topological path in dut (length=12):\n"
    output += json.dumps(stats, indent=2) + "\nEnd of script.\n"
    metrics = parse_synthesis(output, "dut")
    assert metrics.total_cells == 44
    assert metrics.combinational_gates == 5
    assert metrics.flip_flops == 22
    assert metrics.latches == 17
    assert metrics.logic_depth == 12
    assert metrics.cell_breakdown == CELLS


def test_zero_cell_design_is_valid_and_missing_depth_stays_unknown():
    metrics = parse_synthesis('{"design": {"num_cells_by_type": {}}}', "dut")
    assert metrics.total_cells == 0
    assert metrics.logic_depth is None


@pytest.mark.parametrize("output", ["", '{"modules":', '{"modules": {}}'])
def test_missing_or_incomplete_statistics_are_not_silently_zero(output):
    with pytest.raises(ValueError, match="cell counts"):
        parse_synthesis(output, "dut")


@pytest.mark.eda
def test_real_latch_is_rejected_after_simulation_passes(tmp_path, eda_tools):
    rtl = tmp_path / "dut.sv"
    rtl.write_text(
        "module dut(input en, input a, output reg y);\nalways @* if (en) y = a;\nendmodule\n"
    )
    tb = tmp_path / "tb.sv"
    tb.write_text(
        "module tb; reg en=0, a=0; wire y; dut d(en,a,y); initial begin\n"
        '$dumpfile("waves.vcd"); $dumpvars(0,tb);\n'
        '#1; en=1; a=1; #1; if (y !== 1) $fatal(1,"set");\n'
        'en=0; a=0; #1; if (y !== 1) $fatal(1,"hold");\n'
        '$display("RTL_AGENT_PASS"); $finish; end endmodule\n'
    )
    results, metrics = EDAToolchain(RunConfig(top="dut")).verify(tmp_path, rtl, tb)
    by_stage = {t.stage: t for t in results}
    assert by_stage["simulate"].passed
    assert by_stage["synth"].returncode == 0
    assert not by_stage["synth"].passed
    assert metrics.latches == 1 and metrics.flip_flops == 0
    assert any(
        "1 accidental level-sensitive latches" in d.message for d in by_stage["synth"].diagnostics
    )
    assert (tmp_path / "synth.log").is_file()


@pytest.mark.eda
def test_hierarchical_design_is_flattened_and_counted(tmp_path, eda_tools):
    rtl = tmp_path / "dut.sv"
    rtl.write_text(
        "module bit_and(input a,b, output y); assign y=a&b; endmodule\n"
        "module dut(input a,b,c, output y); wire w; bit_and u(a,b,w); "
        "assign y=w^c; endmodule\n"
    )
    tb = tmp_path / "tb.sv"
    tb.write_text(
        "module tb; reg a=1,b=1,c=0; wire y; dut d(a,b,c,y); initial begin\n"
        '#1; if (y !== 1) $fatal(1,"bad output");\n'
        '$display("RTL_AGENT_PASS"); $finish; end endmodule\n'
    )
    results, metrics = EDAToolchain(RunConfig(top="dut")).verify(tmp_path, rtl, tb)
    assert all(t.passed for t in results)
    assert metrics.total_cells == metrics.combinational_gates == 2
    assert metrics.logic_depth == 2
    assert metrics.flip_flops == metrics.latches == 0
    assert (tmp_path / "netlist.v").is_file()


def test_successful_yosys_exit_without_statistics_is_rejected(tmp_path, eda_tools, monkeypatch):
    def command(args, *, stage, **kwargs):
        return ToolResult(
            stage=stage, command=args, returncode=0, duration_seconds=0, output="", passed=True
        )

    monkeypatch.setattr("rtl_agent.tools.run_command", command)
    results, metrics = EDAToolchain(RunConfig(top="dut")).verify(
        tmp_path, tmp_path / "dut.sv", tmp_path / "tb.sv"
    )
    assert results[-1].stage == "synth" and not results[-1].passed
    assert metrics is None
    assert "cell counts" in results[-1].diagnostics[-1].message
