import pytest

from rtl_agent.web_artifacts import waveform, waveform_with_reason


def test_real_vcd_timescale_aliases_vectors_and_unknowns_are_preserved(tmp_path):
    path = tmp_path / "waves.vcd"
    path.write_text("""$timescale 10 ps $end
$scope module tb $end
$var wire 1 ! clk $end
$var wire 64 " wide [63:0] $end
$scope module dut $end
$var wire 1 ! clk $end
$upscope $end
$upscope $end
$enddefinitions $end
#0
x!
bxz "
#100
1!
b1111111111111111111111111111111111111111111111111111111111111111 "
#200
0!
""")
    wave = waveform(path)
    assert wave["timescale"] == "10ps"
    assert wave["duration_ns"] == 2
    assert len(wave["channels"]) == 2
    assert wave["channels"][0]["samples"] == [[0, "x"], [1, 1], [2, 0]]
    assert wave["channels"][1]["width"] == 64
    assert wave["channels"][1]["samples"][-1][1] == "b" + "1" * 64


def test_missing_timescale_or_missing_file_does_not_guess_samples(tmp_path):
    path = tmp_path / "waves.vcd"
    assert waveform(path) is None
    path.write_text("$var wire 1 ! a $end\n$enddefinitions $end\n#0\n0!\n#10\n1!")
    assert waveform(path) is None


def test_unknown_initial_values_are_not_backfilled(tmp_path):
    path = tmp_path / "waves.vcd"
    path.write_text("""$timescale 1ns $end
$var wire 1 ! a $end
$enddefinitions $end
#0
#10
1!
#20
""")
    wave = waveform(path)
    assert wave["start_ns"] == 0
    assert wave["channels"][0]["samples"] == [[10, 1]]


@pytest.mark.parametrize(
    "content,reason",
    [
        (None, "No waves.vcd"),
        ("", "empty"),
        ("$var wire 1 ! clk $end\n", "header is incomplete"),
        ("$var wire 1 ! clk $end\n$enddefinitions $end\n#0\n0!\n#1\n1!", "time unit is unknown"),
        (
            "$timescale 1ns $end\n$var wire 1 ! clk $end\n$enddefinitions $end\n#10\n0!\n#5\n1!",
            "move backwards",
        ),
        (
            "$timescale 1ns $end\n$var wire 1 ! clk $end\n$enddefinitions $end\n#0\n0!",
            "only one time instant",
        ),
    ],
)
def test_absent_or_rejected_waveforms_have_precise_display_reasons(tmp_path, content, reason):
    path = tmp_path / "waves.vcd"
    if content is not None:
        path.write_text(content)
    wave, explanation = waveform_with_reason(path)
    assert wave is None and reason in explanation
    assert waveform(path) is None


def test_seconds_timescale_keeps_its_real_large_nanosecond_range(tmp_path):
    path = tmp_path / "waves.vcd"
    path.write_text(
        "$timescale 1s $end\n$var wire 1 ! clk $end\n$enddefinitions $end\n#0\n0!\n#1396\n1!\n"
    )
    wave, explanation = waveform_with_reason(path)
    assert explanation is None
    assert wave["duration_ns"] == 1396000000000
    assert wave["channels"][0]["samples"] == [[0, 0], [1396000000000, 1]]
