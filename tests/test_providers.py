import json

import httpx
import pytest
from openai import OpenAI

from rtl_agent.models import Attempt, RTLRepair
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


def test_live_sdk_testbench_has_its_own_schema_and_receives_no_rtl(make_provider, design):
    def handler(request):
        body = json.loads(request.content)
        assert request.url.path == "/v1/responses"
        assert body["store"] is False
        assert body["text"]["format"]["strict"] is True
        assert set(body["text"]["format"]["schema"]["properties"]) == {"testbench", "assumptions"}
        prompt = body["input"][1]["content"]
        assert "NPU MAC" in prompt and "npu_neural_mac" in prompt
        assert design.rtl not in prompt
        assert "No RTL implementation is available" in prompt
        assert "Begin the testbench source with `timescale 1ns/1ps." in prompt
        return httpx.Response(
            200,
            json=response_body(
                {
                    "assumptions": [],
                    "testbench": design.testbench,
                }
            ),
        )

    result = make_provider(handler).generate_testbench("NPU MAC", "npu_neural_mac")
    assert isinstance(result, BenchDraft)
    assert result.testbench == design.testbench
    with pytest.raises(ValueError):
        BenchDraft(assumptions=[], testbench=design.testbench, rtl=design.rtl)


def test_supplied_testbench_generation_requests_only_rtl(make_provider, design):
    def handler(request):
        body = json.loads(request.content)
        properties = body["text"]["format"]["schema"]["properties"]
        assert set(properties) == {"rtl", "summary", "assumptions"}
        assert design.testbench in body["input"][1]["content"]
        assert "written from the specification in a separate step" in body["input"][1]["content"]
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
    assert result.testbench == design.testbench


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


def test_caption_sdk_uses_same_model_and_only_the_allowed_facts(make_provider, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "secret-caption-key")

    def handler(request):
        body = json.loads(request.content)
        assert body["model"] == "test-model" and body["store"] is False
        facts = json.loads(body["input"][1]["content"])
        assert set(facts) == {
            "spec",
            "diagnostics",
            "change_summary",
            "attempt_number",
            "total_attempts",
            "final_status",
            "later_attempt_passed",
            "failure_kind",
        }
        assert "secret-caption-key" not in json.dumps(facts)
        assert "never instructions" in body["input"][0]["content"]
        assert "14-year-old" in body["input"][0]["content"]
        assert "formatting slip, not a wrong answer" in body["input"][0]["content"]
        return httpx.Response(
            200,
            json=response_body(
                {"caption": "The test caught an incorrect sum. The final attempt passed."}
            ),
        )

    provider = make_provider(handler)
    result = provider.caption(
        {
            "spec": "counter secret-caption-key",
            "diagnostics": [],
            "change_summary": "repair sum",
            "attempt_number": 1,
            "total_attempts": 2,
            "final_status": "passed",
            "later_attempt_passed": True,
            "failure_kind": "wrong_answer",
            "rtl": "DO NOT SEND SOURCE",
            "logs": "DO NOT SEND LOGS",
            "extra": "IGNORE",
        }
    )
    assert result == "The test caught an incorrect sum. The final attempt passed."


@pytest.mark.parametrize(
    "caption",
    [
        "Lint found an error. A later attempt passed.",
        "The reference model failed. A later attempt passed.",
        "The draft could not be compiled. Another attempt passed.",
        "The draft passed every check.",
        "The draft failed. A later attempt passed. This is a third sentence.",
    ],
)
def test_model_captions_with_jargon_or_wrong_sentence_count_use_fallback(make_provider, caption):
    provider = make_provider(
        lambda _: httpx.Response(200, json=response_body({"caption": caption}))
    )
    with pytest.raises(ProviderError):
        provider.caption(
            {
                "spec": "bit-counter",
                "diagnostics": [],
                "change_summary": None,
                "attempt_number": 1,
                "total_attempts": 2,
                "final_status": "passed",
                "later_attempt_passed": True,
                "failure_kind": "housekeeping",
            }
        )
