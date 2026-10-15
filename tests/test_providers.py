import json

import httpx
import pytest
from openai import OpenAI

from rtl_agent.models import Attempt, DesignDraft, RTLRepair
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


def test_live_sdk_generation_serialization_and_pydantic_parsing(make_provider, design):
    def handler(request):
        body = json.loads(request.content)
        assert request.url.path == "/v1/responses"
        assert body["store"] is False
        assert body["text"]["format"]["strict"] is True
        assert set(body["text"]["format"]["schema"]["properties"]) == {
            "rtl",
            "testbench",
            "summary",
            "assumptions",
        }
        return httpx.Response(
            200,
            json=response_body(
                {
                    "summary": "NPU MAC",
                    "assumptions": [],
                    "rtl": design.rtl,
                    "testbench": design.testbench,
                }
            ),
        )

    result = make_provider(handler).generate("NPU MAC", "npu_neural_mac")
    assert isinstance(result, DesignDraft)
    assert result.rtl == design.rtl


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
        provider.generate("NPU MAC", "npu_neural_mac")
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
        provider.generate("NPU MAC", "npu_neural_mac")
