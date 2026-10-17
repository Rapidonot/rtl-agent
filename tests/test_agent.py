import hashlib
import json
from pathlib import Path

import pytest

from rtl_agent.agent import load_report
from rtl_agent.providers import ProviderError

SPEC = "Signed INT8 NPU MAC with synchronous clear and enable."
pytestmark = pytest.mark.eda


def test_real_npu_mac_passes_all_four_stages(make_agent, design):
    report = make_agent().run(SPEC)
    assert report.passed, report.error
    assert all(tool.passed for tool in report.attempts[0].tools)
    assert [t.stage for t in report.attempts[0].tools] == ["lint", "compile", "simulate", "synth"]
    assert report.synthesis == report.attempts[0].synthesis
    assert report.synthesis.flip_flops == 20
    assert report.synthesis.combinational_gates > 0
    assert report.synthesis.logic_depth > 0
    assert report.synthesis.latches == 0
    directory = Path(report.run_dir)
    assert (directory / "npu_neural_mac.sv").read_text() == design.rtl
    assert (directory / "waves.vcd").stat().st_size > 0
    assert (directory / "netlist.v").stat().st_size > 0
    assert (directory / "attempts/01/synth.log").is_file()
    assert "Synthesis | Gates | DFFs | Depth" in (directory / "report.md").read_text()
    assert load_report(directory).status == "passed"


def test_functional_failure_repairs_rtl_without_changing_testbench(make_agent, design, provider):
    design.rtl = design.rtl.replace("acc +", "acc -")
    report = make_agent().run(SPEC)
    assert [a.passed for a in report.attempts] == [False, True]
    failed_simulation = report.attempts[0].tools[-1]
    assert failed_simulation.stage == "simulate" and failed_simulation.returncode != 0
    assert "expected=12 actual=-12" in failed_simulation.output
    assert report.attempts[0].waveform_trace
    assert "0xffff4" in report.attempts[0].waveform_trace
    assert "#" in report.attempts[0].waveform_trace
    assert report.attempts[0].synthesis is None
    provider.repair.assert_called_once()
    assert provider.repair.call_args.args[3] == design.testbench
    directory = Path(report.run_dir)
    for path in [directory / "tb.sv", *sorted((directory / "attempts").glob("*/tb.sv"))]:
        assert path.read_bytes() == design.testbench.encode()
        assert hashlib.sha256(path.read_bytes()).hexdigest() == report.testbench_sha256
    assert all(a.testbench_sha256 == report.testbench_sha256 for a in report.attempts)


def test_default_timescale_is_fixed_before_real_verification_and_every_repair(
    make_agent, design, provider
):
    from rtl_agent.web_artifacts import waveform

    design.testbench = design.testbench.replace("`timescale 1ns/1ps\n", "")
    expected = "`timescale 1ns/1ps\n" + design.testbench
    design.rtl = design.rtl.replace("acc +", "acc -")
    report = make_agent().run(SPEC)
    assert [a.passed for a in report.attempts] == [False, True]
    assert provider.generate.call_args.args[2] == expected
    assert provider.repair.call_args.args[3] == expected
    for path in Path(report.run_dir).rglob("tb.sv"):
        assert path.read_bytes() == expected.encode()
        assert hashlib.sha256(path.read_bytes()).hexdigest() == report.testbench_sha256
    wave = waveform(Path(report.run_dir) / "attempts/02/waves.vcd")
    assert wave is not None and wave["timescale"] == "1ps"


def test_attempt_budget_is_total_candidates(make_agent, design, provider):
    design.rtl = design.rtl.replace("acc +", "acc -")
    report = make_agent(max_attempts=1).run(SPEC)
    assert report.status == "failed" and len(report.attempts) == 1
    assert report.synthesis is None
    provider.repair.assert_not_called()
    assert not (Path(report.run_dir) / "npu_neural_mac.sv").exists()


def test_syntax_error_is_recorded_and_repaired(make_agent, design, provider):
    design.rtl = design.rtl.replace("if (clear)", "if (clear")
    report = make_agent().run(SPEC)
    assert report.passed and len(report.attempts) == 2
    tools = report.attempts[0].tools
    assert {tool.stage for tool in tools} == {"lint", "compile"}
    assert not any(tool.passed for tool in tools)
    assert any(d.line is not None for tool in tools for d in tool.diagnostics)
    assert provider.repair.call_args.args[-1] == report.attempts[0]


def test_latch_warning_requires_repair_even_when_simulation_passes(make_agent, design):
    design.rtl = design.rtl.replace(
        "    always @(posedge clk)",
        "    reg latched_en;\n"
        "    always @* begin\n"
        "        if (en) latched_en = en;\n"
        "    end\n"
        "    always @(posedge clk)",
    ).replace("else if (en)", "else if (en && latched_en)")
    report = make_agent().run(SPEC)
    assert report.passed and len(report.attempts) == 2
    lint, compile_result, simulation, _ = report.attempts[0].tools
    assert not lint.passed and "LATCH" in lint.output
    assert compile_result.passed and simulation.passed


def test_invalid_testbench_stops_without_repair(make_agent, design, provider):
    design.testbench = "module tb; initial $finish; endmodule"
    report = make_agent().run(SPEC)
    assert report.status == "error" and report.attempts == []
    assert "Initial testbench rejected" in report.error
    provider.generate.assert_not_called()
    provider.repair.assert_not_called()


def test_provider_failure_preserves_partial_run(make_agent, design, provider):
    design.rtl = design.rtl.replace("acc +", "acc -")
    provider.repair.side_effect = ProviderError("Simulated network error")
    report = make_agent().run(SPEC)
    assert report.status == "error" and len(report.attempts) == 1
    assert report.error == "Simulated network error"
    directory = Path(report.run_dir)
    assert json.loads((directory / "report.json").read_text())["status"] == "error"
    assert (directory / "attempts/01/simulate.log").exists()


def test_rtl_simulation_control_is_rejected_before_execution(make_agent, design):
    design.rtl = design.rtl.replace("endmodule", "initial $finish;\nendmodule")
    report = make_agent().run(SPEC)
    assert report.passed
    assert report.attempts[0].diagnostics and report.attempts[0].tools == []


def test_supplied_testbench_is_used_for_generation_and_every_repair(make_agent, design, provider):
    reference = design.testbench + "\n// User reference testbench.\n"
    design.testbench = "model output must not replace reference"
    design.rtl = design.rtl.replace("acc +", "acc -")
    report = make_agent().run(SPEC, testbench=reference)
    assert [a.passed for a in report.attempts] == [False, True]
    provider.generate.assert_called_once_with(SPEC, "npu_neural_mac", reference)
    provider.generate_testbench.assert_not_called()
    assert provider.repair.call_args.args[3] == reference
    for path in Path(report.run_dir).rglob("tb.sv"):
        assert path.read_text() == reference


def test_invalid_supplied_testbench_is_rejected_before_api(make_agent, provider):
    with pytest.raises(ValueError, match="Supplied testbench rejected"):
        make_agent().run(SPEC, testbench="module tb; endmodule")
    provider.generate.assert_not_called()
    provider.generate_testbench.assert_not_called()


def test_stage1_reports_remain_readable(make_agent):
    report = make_agent().run(SPEC)
    legacy = report.model_dump()
    legacy.pop("synthesis")
    for attempt in legacy["attempts"]:
        attempt.pop("synthesis")
        attempt.pop("waveform_trace")
    directory = Path(report.run_dir)
    (directory / "legacy.json").write_text(json.dumps(legacy))
    assert load_report(directory / "legacy.json").synthesis is None


def test_synthesis_failure_cannot_pass_run(make_agent):
    agent = make_agent(max_attempts=1)
    agent.toolchain.paths["yosys"] = "/usr/bin/false"
    report = agent.run(SPEC)
    assert report.status == "failed"
    assert all(t.passed for t in report.attempts[0].tools[:3])
    assert not report.attempts[0].tools[-1].passed
    assert report.attempts[0].tools[-1].stage == "synth"
    assert report.synthesis is None
    assert not (Path(report.run_dir) / "npu_neural_mac.sv").exists()
