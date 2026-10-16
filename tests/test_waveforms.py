import pytest

from rtl_agent.models import Attempt, ToolResult
from rtl_agent.prompts import repair_prompt
from rtl_agent.tools import extract_vcd_trace


def test_vcd_tail_carries_values_and_handles_vectors_aliases_and_unknowns(tmp_path):
    vcd = tmp_path / "waves.vcd"
    vcd.write_text("""$timescale
1 ps
$end
$scope module tb $end
$var reg 1 ! clk $end
$var wire 8 " data [7:0] $end
$scope module dut $end
$var wire 8 " data [7:0] $end
$var wire 1 # ready $end
$upscope $end
$upscope $end
$enddefinitions $end
#0
$dumpvars
0!
bx "
x#
$end
#5
1!
b00001010 "
1#
#10
0!
#15
1!
bzzzzzzzz "
#20
0!
""")
    trace = extract_vcd_trace(vcd, max_timestamps=3)
    assert trace.splitlines() == [
        "Time (1ps) | tb.clk | tb.data[7:0] | tb.dut.ready",
        "--- | --- | --- | ---",
        "#10 | 0 | 0x0a | 1",
        "#15 | 1 | bzzzzzzzz | 1",
        "#20 | 0 | bzzzzzzzz | 1",
    ]
    assert "#0 | 0 | bx | x" in extract_vcd_trace(vcd)
    assert extract_vcd_trace(vcd, max_timestamps=0) == ""


@pytest.mark.parametrize("content", ["", "truncated header", "$var wire bad ! x $end"])
def test_empty_or_invalid_waveforms_do_not_mask_simulation_failure(tmp_path, content):
    vcd = tmp_path / "waves.vcd"
    assert extract_vcd_trace(vcd) == ""
    vcd.write_text(content)
    assert extract_vcd_trace(vcd) == ""


@pytest.mark.parametrize("passed", [False, True])
def test_waveforms_enter_repair_prompt_only_for_failed_simulation(passed):
    attempt = Attempt(
        number=1,
        rtl_sha256="a",
        testbench_sha256="b",
        change_summary="draft",
        passed=False,
        waveform_trace="#40 | 0x04 | 0x05",
        tools=[
            ToolResult(
                stage="simulate",
                command=["vvp"],
                returncode=int(not passed),
                duration_seconds=0,
                output="",
                passed=passed,
            )
        ],
    )
    prompt = repair_prompt("counter", "dut", "rtl", "tb", attempt)
    assert ("<waveform_trace>\n#40 | 0x04 | 0x05\n</waveform_trace>" in prompt) is not passed
