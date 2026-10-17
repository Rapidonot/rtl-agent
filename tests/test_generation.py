import hashlib
from pathlib import Path
from unittest.mock import Mock

import pytest

from rtl_agent.agent import RTLAgent, ensure_testbench_timescale, load_report
from rtl_agent.control import RunControl
from rtl_agent.models import RunConfig
from rtl_agent.models import TestbenchDraft as BenchDraft
from rtl_agent.providers import ProviderError


@pytest.fixture
def agent(tmp_path, monkeypatch, provider):
    toolchain = Mock()
    toolchain.verify.return_value = ([], None)
    monkeypatch.setattr("rtl_agent.agent.EDAToolchain", lambda _: toolchain)
    return RTLAgent(provider, RunConfig(top="npu_neural_mac", max_attempts=1, output_dir=tmp_path))


def test_testbench_is_generated_and_hash_locked_before_rtl(agent, provider, design):
    reference = design.testbench
    provider.generate_testbench.side_effect = lambda *_: BenchDraft(
        assumptions=["First assumption", "Shared assumption", "First assumption"],
        testbench=reference,
    )
    design.assumptions = ["Shared assumption", "RTL assumption"]
    # Even an incorrect provider cannot replace the already frozen testbench.
    design.testbench = "unexpected replacement"

    def generate(spec, top, testbench):
        assert spec == "NPU specification" and top == "npu_neural_mac"
        assert testbench == reference
        directory = next(agent.config.output_dir.iterdir())
        assert (directory / "tb.sv").read_bytes() == reference.encode()
        report = load_report(directory)
        assert report.testbench_sha256 == hashlib.sha256(reference.encode()).hexdigest()
        assert report.assumptions == ["First assumption", "Shared assumption"]
        assert report.status == "running" and not report.attempts
        return design

    provider.generate.side_effect = generate
    report = agent.run("NPU specification")
    assert report.status == "failed"
    assert report.assumptions == ["First assumption", "Shared assumption", "RTL assumption"]
    assert [c[0] for c in provider.mock_calls[:2]] == ["generate_testbench", "generate"]
    provider.generate_testbench.assert_called_once_with("NPU specification", "npu_neural_mac")
    for path in Path(report.run_dir).rglob("tb.sv"):
        assert path.read_text() == reference
    assert (Path(report.run_dir) / "testbench.json").is_file()


def test_invalid_generated_testbench_stops_before_rtl_generation(agent, provider):
    provider.generate_testbench.side_effect = lambda *_: BenchDraft(
        assumptions=[], testbench="module tb; endmodule"
    )
    report = agent.run("NPU specification")
    assert report.status == "error" and "Initial testbench rejected" in report.error
    provider.generate.assert_not_called()
    agent.toolchain.verify.assert_not_called()


def test_testbench_request_failure_never_starts_rtl_generation(agent, provider):
    provider.generate_testbench.side_effect = ProviderError("Testbench request failed")
    report = agent.run("NPU specification")
    assert report.status == "error" and report.error == "Testbench request failed"
    provider.generate.assert_not_called()


def test_rtl_request_failure_preserves_frozen_testbench(agent, provider, design):
    provider.generate.side_effect = ProviderError("RTL request failed")
    report = agent.run("NPU specification")
    assert report.status == "error" and report.testbench_sha256
    assert (Path(report.run_dir) / "tb.sv").read_text() == design.testbench
    assert load_report(Path(report.run_dir)).testbench_sha256 == report.testbench_sha256


def test_cancellation_between_generation_calls_prevents_rtl_request(agent, provider, design):
    agent.control = RunControl(10)

    def testbench(*_):
        agent.control.cancelled.set()
        return BenchDraft(assumptions=[], testbench=design.testbench)

    provider.generate_testbench.side_effect = testbench
    report = agent.run("NPU specification")
    assert report.status == "interrupted"
    provider.generate.assert_not_called()


def test_default_agent_never_starts_or_builds_captions(agent, provider, monkeypatch):
    start, fallback = Mock(), Mock()
    monkeypatch.setattr("rtl_agent.agent.start_caption", start)
    monkeypatch.setattr("rtl_agent.agent.fallback_caption", fallback)
    report = agent.run("NPU specification")
    assert report.status == "failed" and not agent.captions
    assert report.caption is None and report.caption_source is None and not report.caption_pending
    start.assert_not_called()
    fallback.assert_not_called()
    provider.caption.assert_not_called()


def test_explicit_caption_opt_in_saves_fallback_and_starts_request(
    tmp_path, provider, agent, monkeypatch
):
    start = Mock()
    monkeypatch.setattr("rtl_agent.agent.start_caption", start)
    enabled = RTLAgent(provider, agent.config, captions=True)
    report = enabled.run("NPU specification")
    assert report.status == "failed" and report.caption_pending
    assert report.caption and report.caption_source == "deterministic"
    assert load_report(Path(report.run_dir)).caption == report.caption
    start.assert_called_once()


@pytest.mark.parametrize("supplied", [False, True])
def test_missing_timescale_is_added_before_generation_and_hash_lock(
    agent, provider, design, supplied
):
    raw = design.testbench.replace("`timescale 1ns/1ps\n", "")
    expected = "`timescale 1ns/1ps\n" + raw
    provider.generate_testbench.side_effect = lambda *_: BenchDraft(assumptions=[], testbench=raw)
    report = agent.run("NPU specification", testbench=raw if supplied else None)
    assert report.status == "failed"  # Display preparation cannot manufacture a pass.
    assert provider.generate.call_args.args[2] == expected
    assert report.testbench_sha256 == hashlib.sha256(expected.encode()).hexdigest()
    for path in Path(report.run_dir).rglob("tb.sv"):
        assert path.read_bytes() == expected.encode()
    assert all(a.testbench_sha256 == report.testbench_sha256 for a in report.attempts)
    if supplied:
        provider.generate_testbench.assert_not_called()


@pytest.mark.parametrize("prefix", ["", "// `timescale 10s/1s\n", "/* `timescale 10s/1s */\n"])
def test_comment_directives_cannot_suppress_the_default_timescale(prefix):
    source = prefix + "module tb; endmodule\n"
    fixed = ensure_testbench_timescale(source)
    assert fixed == "`timescale 1ns/1ps\n" + source
    assert ensure_testbench_timescale(fixed) == fixed


def test_active_explicit_timescale_and_source_bytes_are_preserved():
    source = "// fixed timing\n`timescale 10us/1ns\nmodule tb; endmodule\n"
    assert ensure_testbench_timescale(source) == source
