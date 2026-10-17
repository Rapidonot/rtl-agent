import pytest

from rtl_agent.policy import validate_source, validate_top

TOP = "npu_neural_mac"


def test_valid_npu_mac_and_testbench(design):
    assert validate_source(design.rtl, top=TOP) == []
    assert validate_source(design.testbench, top=TOP, is_testbench=True) == []


def test_timescale_is_a_prompt_preference_not_a_policy_requirement(design):
    source = design.testbench.replace("`timescale 1ns/1ps\n", "")
    assert validate_source(source, top=TOP, is_testbench=True) == []


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


FORBIDDEN_TASKS = [
    "$system",
    "$fopen",
    "$fclose",
    "$fwrite",
    "$fdisplay",
    "$fscanf",
    "$readmemh",
    "$readmemb",
    "$writememh",
    "$writememb",
]


@pytest.mark.parametrize("is_testbench", [False, True], ids=["rtl", "testbench"])
@pytest.mark.parametrize("task", FORBIDDEN_TASKS)
def test_external_file_and_system_tasks_are_forbidden_in_both_sources(design, is_testbench, task):
    source = design.testbench if is_testbench else design.rtl
    source = source.replace("endmodule", f'{task}("outside");\nendmodule')
    problems = validate_source(source, top=TOP, is_testbench=is_testbench)
    assert any(task in problem for problem in problems)


@pytest.mark.parametrize("is_testbench", [False, True], ids=["rtl", "testbench"])
@pytest.mark.parametrize("directive", ['`include "outside.sv"', "`include <outside.sv>"])
def test_include_directives_are_forbidden_in_both_sources(design, is_testbench, directive):
    source = design.testbench if is_testbench else design.rtl
    problems = validate_source(directive + "\n" + source, top=TOP, is_testbench=is_testbench)
    assert any("`include" in problem for problem in problems)


@pytest.mark.parametrize("is_testbench", [False, True], ids=["rtl", "testbench"])
def test_forbidden_names_in_comments_and_strings_are_ignored(design, is_testbench):
    source = design.testbench if is_testbench else design.rtl
    names = " ".join([*FORBIDDEN_TASKS, "`include outside.sv", "$dumpfile(bad_path)"])
    harmless = f'// {names}\n/* {names} */\nlocalparam EXPLANATION = "{names}";\n'
    source = source.replace("endmodule", harmless + "endmodule")
    assert validate_source(source, top=TOP, is_testbench=is_testbench) == []


@pytest.mark.parametrize(
    "argument",
    [
        '"other.vcd"',
        '"../waves.vcd"',
        '"/tmp/waves.vcd"',
        "path",
        '"waves.vcd", 1',
        '{"waves", ".vcd"}',
        '"waves.vcd" + suffix',
        "",
    ],
)
def test_every_dumpfile_must_use_exactly_the_local_waveform_name(design, argument):
    # Retain the valid call: an additional unsafe call must still be rejected.
    source = design.testbench.replace(
        '$dumpfile("waves.vcd");',
        f'$dumpfile("waves.vcd");\n$dumpfile({argument});',
    )
    problems = validate_source(source, top=TOP, is_testbench=True)
    assert any('argument must be exactly "waves.vcd"' in problem for problem in problems)


def test_dumpfile_allows_whitespace_and_comments_around_the_exact_argument(design):
    source = design.testbench.replace(
        '$dumpfile("waves.vcd")', '$dumpfile /* waveform */ (\n "waves.vcd" /* path */ )'
    )
    assert validate_source(source, top=TOP, is_testbench=True) == []
