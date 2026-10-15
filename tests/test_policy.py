import pytest

from rtl_agent.policy import validate_source, validate_top

TOP = "npu_neural_mac"


def test_valid_npu_mac_and_testbench(design):
    assert validate_source(design.rtl, top=TOP) == []
    assert validate_source(design.testbench, top=TOP, is_testbench=True) == []


@pytest.mark.parametrize(
    "fragment",
    [
        "initial $finish;",
        "/* verilator lint_off WIDTH */",
        'always @* $display("RTL_AGENT_PASS");',
    ],
)
def test_rtl_cannot_bypass_real_verification(design, fragment):
    assert validate_source(design.rtl.replace("endmodule", fragment + "\nendmodule"), top=TOP)


@pytest.mark.parametrize(
    "source",
    [
        "",
        "module wrong; endmodule",
        "```verilog\nmodule dut; endmodule\n```",
    ],
)
def test_source_requires_raw_code_and_correct_top(source):
    assert validate_source(source, top="dut")


@pytest.mark.parametrize(
    "task",
    [
        "$fatal",
        '$display("RTL_AGENT_PASS")',
        "$finish",
        '$dumpfile("waves.vcd")',
    ],
)
def test_testbench_requires_each_protocol_element(design, task):
    testbench = design.testbench.replace(task, "removed_task")
    assert validate_source(testbench, top=TOP, is_testbench=True)


def test_comment_cannot_supply_success_marker(design):
    testbench = design.testbench.replace(
        '$display("RTL_AGENT_PASS");',
        '// $display("RTL_AGENT_PASS");',
    )
    assert validate_source(testbench, top=TOP, is_testbench=True)


def test_comments_do_not_create_false_initial_blocks(design):
    assert (
        validate_source("// initial setup is handled by the testbench\n" + design.rtl, top=TOP)
        == []
    )


@pytest.mark.parametrize("top", ["tb", "../dut", "dut; ls", "-I/tmp", ""])
def test_top_name_cannot_change_commands_or_paths(top):
    with pytest.raises(ValueError):
        validate_top(top)
