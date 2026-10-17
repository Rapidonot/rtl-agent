import shutil
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from rtl_agent import RTLAgent, RunConfig
from rtl_agent.models import RTLDesign, RTLRepair
from rtl_agent.models import TestbenchDraft as BenchDraft
from rtl_agent.providers import Provider

FIXTURES = Path(__file__).parent / "fixtures"
TOP = "npu_neural_mac"


@pytest.fixture
def eda_tools():
    if any(shutil.which(tool) is None for tool in ("iverilog", "vvp", "verilator", "yosys")):
        pytest.skip("Real EDA integration requires iverilog, vvp, verilator and yosys on PATH")


@pytest.fixture
def design():
    return SimpleNamespace(
        summary="Signed INT8 NPU MAC.",
        assumptions=[],
        rtl=(FIXTURES / "npu_neural_mac.sv").read_text(),
        testbench=(FIXTURES / "npu_neural_mac_tb.sv").read_text(),
    )


@pytest.fixture
def provider(design):
    provider = Mock(spec=Provider)
    provider.name, provider.model = "test", "fixture"
    provider.generate_testbench.side_effect = lambda spec, top: BenchDraft(
        assumptions=[], testbench=design.testbench
    )
    provider.generate.side_effect = lambda spec, top, testbench: RTLDesign(
        summary=design.summary, assumptions=design.assumptions, rtl=design.rtl
    )
    provider.repair.return_value = RTLRepair(change_summary="Fix RTL defect.", rtl=design.rtl)
    return provider


@pytest.fixture
def make_agent(tmp_path, provider, eda_tools):
    def build(**options):
        return RTLAgent(provider, RunConfig(top=TOP, output_dir=tmp_path, **options))

    return build
