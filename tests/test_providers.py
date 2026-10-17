import json
from pathlib import Path

import httpx
import pytest
from openai import OpenAI

from rtl_agent import RTLAgent, RunConfig
from rtl_agent.models import Attempt, RTLDesign, RTLRepair
from rtl_agent.models import TestbenchDraft as BenchDraft
from rtl_agent.providers import OpenAIProvider, ProviderError


def response_body(payload):
    return {
        "id": "resp_test",
        "object": "response",
        "created_at": 1700000000,
        "model": "test-model",
        "status": "completed",
        "error": None,
        "incomplete_details": None,
        "output": [
            {
                "id": "msg_test",
                "type": "message",
                "role": "assistant",
                "status": "completed",
                "content": [
                    {"type": "output_text", "text": json.dumps(payload), "annotations": []}
                ],
            }
        ],
    }


@pytest.fixture
def make_provider():
    clients = []

    def build(handler):
        client = OpenAI(
            api_key="test-key-never-sent-to-network",
            max_retries=0,
            http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        )
        clients.append(client)
        return OpenAIProvider("test-model", client=client)

    yield build
    for client in clients:
        client.close()


def test_sdk_testbench_and_rtl_use_separate_stateless_requests(make_provider, design):
    requests = []

    def handler(request):
        body = json.loads(request.content)
        requests.append(body)
        assert request.url.path == "/v1/responses"
        assert body["store"] is False
        assert body["text"]["format"]["strict"] is True
        assert "previous_response_id" not in body and "conversation" not in body
        assert [message["role"] for message in body["input"]] == ["system", "user"]
        properties = set(body["text"]["format"]["schema"]["properties"])
        prompt = body["input"][1]["content"]
        if len(requests) == 1:
            assert properties == {"assumptions", "testbench"}
            assert design.rtl not in prompt and design.testbench not in prompt
            assert "independent behavioral reference model" in prompt
            payload = {"assumptions": [], "testbench": design.testbench}
        else:
            assert properties == {"summary", "assumptions", "rtl"}
            assert design.testbench in prompt
            payload = {"summary": design.summary, "assumptions": [], "rtl": design.rtl}
        return httpx.Response(200, json=response_body(payload))

    provider = make_provider(handler)
    bench = provider.generate_testbench("NPU MAC", "npu_neural_mac")
    assert isinstance(bench, BenchDraft)
    result = provider.generate("NPU MAC", "npu_neural_mac", bench.testbench)
    assert isinstance(result, RTLDesign)
    assert result.rtl == design.rtl
    assert len(requests) == 2


def test_supplied_testbench_generation_requests_only_rtl(make_provider, design):
    def handler(request):
        body = json.loads(request.content)
        properties = body["text"]["format"]["schema"]["properties"]
        assert set(properties) == {"rtl", "summary", "assumptions"}
        assert design.testbench in body["input"][1]["content"]
        return httpx.Response(
            200,
            json=response_body(
                {
                    "summary": "NPU MAC",
                    "assumptions": [],
                    "rtl": design.rtl,
                }
            ),
        )

    result = make_provider(handler).generate("NPU MAC", "npu_neural_mac", design.testbench)
    assert isinstance(result, RTLDesign)
    assert result.rtl == design.rtl


@pytest.mark.eda
def test_separate_sdk_calls_and_rtl_repair_with_real_eda(
    make_provider, design, tmp_path, eda_tools
):
    calls = []

    def handler(request):
        body = json.loads(request.content)
        properties = set(body["text"]["format"]["schema"]["properties"])
        calls.append(properties)
        prompt = body["input"][1]["content"]
        if len(calls) == 1:
            assert properties == {"assumptions", "testbench"}
            assert design.rtl not in prompt
            payload = {
                "assumptions": ["Accumulator wraps at 20 bits."],
                "testbench": design.testbench,
            }
        else:
            assert design.testbench in prompt
            assert next(tmp_path.iterdir()).joinpath("tb.sv").read_text() == design.testbench
            if len(calls) == 2:
                assert properties == {"summary", "assumptions", "rtl"}
                payload = {
                    "summary": design.summary,
                    "assumptions": [],
                    "rtl": design.rtl.replace("acc +", "acc -"),
                }
            else:
                assert properties == {"change_summary", "rtl"}
                assert "<waveform_trace>" in prompt and "expected=12 actual=-12" in prompt
                payload = {"change_summary": "Restore accumulation.", "rtl": design.rtl}
        return httpx.Response(200, json=response_body(payload))

    report = RTLAgent(
        make_provider(handler), RunConfig(top="npu_neural_mac", output_dir=tmp_path)
    ).run("Signed INT8 NPU MAC with synchronous clear and enable.")
    assert report.passed, report.error
    assert len(calls) == 3
    assert [a.passed for a in report.attempts] == [False, True]
    assert report.assumptions == ["Accumulator wraps at 20 bits."]
    assert all(a.testbench_sha256 == report.testbench_sha256 for a in report.attempts)
    assert (Path(report.run_dir) / "tb.sv").read_text() == design.testbench


def test_repair_sdk_schema_excludes_testbench_and_receives_diagnostics(make_provider, design):
    def handler(request):
        body = json.loads(request.content)
        properties = body["text"]["format"]["schema"]["properties"]
        assert set(properties) == {"rtl", "change_summary"}
        prompt = body["input"][1]["content"]
        assert "fixed_testbench" in prompt
        assert "missing semicolon" in prompt
        return httpx.Response(
            200,
            json=response_body(
                {
                    "rtl": design.rtl,
                    "change_summary": "Fix syntax.",
                }
            ),
        )

    from rtl_agent.models import Diagnostic

    attempt = Attempt(
        number=1,
        rtl_sha256="a",
        testbench_sha256="b",
        change_summary="draft",
        passed=False,
        tools=[],
        diagnostics=[Diagnostic(tool="test", severity="error", message="missing semicolon")],
    )
    result = make_provider(handler).repair(
        "NPU MAC", "npu_neural_mac", "bad RTL", design.testbench, attempt
    )
    assert isinstance(result, RTLRepair)
    with pytest.raises(ValueError):
        RTLRepair(rtl=design.rtl, change_summary="cheat", testbench="weakened tests")


def test_api_error_is_actionable_and_does_not_log_response_body(make_provider):
    provider = make_provider(
        lambda request: httpx.Response(
            401,
            json={"error": {"message": "secret-material", "type": "authentication_error"}},
        )
    )
    with pytest.raises(ProviderError) as exc:
        provider.generate_testbench("NPU MAC", "npu_neural_mac")
    assert "HTTP 401" in str(exc.value)
    assert "secret-material" not in str(exc.value)


def test_missing_key_is_detected_before_network(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(ProviderError, match="OPENAI_API_KEY"):
        OpenAIProvider("test-model")


def test_model_refusal_does_not_produce_artifacts(make_provider):
    body = response_body({})
    body["output"][0]["content"] = [{"type": "refusal", "refusal": "Cannot generate."}]
    provider = make_provider(lambda request: httpx.Response(200, json=body))
    with pytest.raises(ProviderError, match="refused"):
        provider.generate_testbench("NPU MAC", "npu_neural_mac")
