"""OpenAI generation and RTL-only repair using Structured Outputs."""

import os
from typing import Protocol, TypeVar

from openai import OpenAI, OpenAIError
from pydantic import BaseModel, ValidationError

from rtl_agent.models import Attempt, DesignDraft, RTLRepair
from rtl_agent.prompts import SYSTEM, generation_prompt, repair_prompt

ResponseType = TypeVar("ResponseType", bound=BaseModel)


class ProviderError(RuntimeError):
    pass


class Provider(Protocol):
    name: str
    model: str

    def generate(self, spec: str, top: str) -> DesignDraft: ...

    def repair(
        self, spec: str, top: str, rtl: str, testbench: str, attempt: Attempt
    ) -> RTLRepair: ...


class OpenAIProvider:
    name = "openai"

    def __init__(
        self,
        model: str,
        *,
        timeout_seconds: float = 120,
        max_output_tokens: int = 12000,
        client: OpenAI | None = None,
    ):
        if not model.strip():
            raise ProviderError("Set --model or RTL_AGENT_MODEL to an API model ID.")
        if client is None and not os.environ.get("OPENAI_API_KEY"):
            raise ProviderError("Set OPENAI_API_KEY in your shell before live generation.")
        self.model = model
        self.max_output_tokens = max_output_tokens
        self.client = client or OpenAI(timeout=timeout_seconds, max_retries=1)

    def _request(self, schema: type[ResponseType], prompt: str) -> ResponseType:
        try:
            response = self.client.responses.parse(
                model=self.model,
                input=[{"role": "system", "content": SYSTEM}, {"role": "user", "content": prompt}],
                text_format=schema,
                max_output_tokens=self.max_output_tokens,
                store=False,
            )
        except OpenAIError as exc:
            # Avoid persisting API request bodies or credentials in reports.
            status = getattr(exc, "status_code", None)
            detail = f" (HTTP {status})" if status is not None else ""
            raise ProviderError(
                f"OpenAI request failed: {type(exc).__name__}{detail}. "
                "Check API key, model access, quota, connectivity and output token limit."
            ) from exc
        except (ValidationError, ValueError) as exc:
            raise ProviderError("OpenAI returned an invalid structured response.") from exc
        if response.status != "completed" or response.output_parsed is None:
            raise ProviderError(
                "OpenAI response was incomplete or refused. Check the specification and "
                "raise --max-output-tokens if the output was truncated."
            )
        return schema.model_validate(response.output_parsed)

    def generate(self, spec: str, top: str) -> DesignDraft:
        return self._request(DesignDraft, generation_prompt(spec, top))

    def repair(self, spec: str, top: str, rtl: str, testbench: str, attempt: Attempt) -> RTLRepair:
        return self._request(RTLRepair, repair_prompt(spec, top, rtl, testbench, attempt))
