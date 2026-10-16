import shutil
from pathlib import Path
from unittest.mock import Mock

import pytest

from rtl_agent import RTLAgent, RunConfig
from rtl_agent.models import DesignDraft, RTLRepair
from rtl_agent.providers import Provider

FIXTURES = Path(__file__).parent / "fixtures"
TOP = "npu_neural_mac"


@pytest.fixture
def eda_tools():
    if any(shutil.which(tool) is None for tool in ("iverilog", "vvp", "verilator", "yosys")):
        pytest.skip("Real EDA integration requires iverilog, vvp, verilator and yosys on PATH")


@pytest.fixture
def design():
    return DesignDraft(
        summary="Signed INT8 NPU MAC.",
        assumptions=[],
        rtl=(FIXTURES / "npu_neural_mac.sv").read_text(),
        testbench=(FIXTURES / "npu_neural_mac_tb.sv").read_text(),
    )


@pytest.fixture
def provider(design):
    provider = Mock(spec=Provider)
    provider.name, provider.model = "test", "fixture"
    provider.generate.return_value = design
    provider.repair.return_value = RTLRepair(change_summary="Fix RTL defect.", rtl=design.rtl)
    return provider


@pytest.fixture
def make_agent(tmp_path, provider, eda_tools):
    def build(**options):
        return RTLAgent(provider, RunConfig(top=TOP, output_dir=tmp_path, **options))

    return build
