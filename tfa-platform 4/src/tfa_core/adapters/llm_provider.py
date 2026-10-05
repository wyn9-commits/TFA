"""tfa_core.adapters.llm_provider — model-agnostic structured extraction.

Two interchangeable providers behind one interface so the platform can move
between GPT-5.x and Claude (Sonnet 5 / Opus 5) on Microsoft Foundry without
touching pipeline code. Both are authenticated with Entra ID tokens from the
workload's Managed Identity — no API keys.

Accuracy features:
- Azure OpenAI path uses native structured outputs (json_schema, strict=True)
  so the response is guaranteed schema-shaped before we even validate it.
- Anthropic path uses forced tool_choice with the schema as the tool's
  input_schema — the Messages-API equivalent of strict structured output.
- Both paths run at deterministic settings appropriate to the model family.
"""
from __future__ import annotations

import json
import logging
from abc import ABC, abstractmethod
from typing import Any

import httpx

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from azure.core.credentials import TokenCredential

logger = logging.getLogger(__name__)

_COGNITIVE_SCOPE = "https://cognitiveservices.azure.com/.default"


class StructuredLLM(ABC):
    """Send (system, user-parts) and get back a dict guaranteed to parse as JSON."""

    @abstractmethod
    def extract(
        self,
        system_prompt: str,
        user_parts: list[dict[str, Any]],
        json_schema: dict[str, Any],
        schema_name: str,
        max_output_tokens: int,
    ) -> dict[str, Any]:
        ...


def _bearer(credential: "TokenCredential") -> dict[str, str]:
    token = credential.get_token(_COGNITIVE_SCOPE)
    return {"Authorization": f"Bearer {token.token}"}


class AzureOpenAIStructuredLLM(StructuredLLM):
    """GPT-5.x on Foundry via chat completions + response_format json_schema."""

    def __init__(self, endpoint: str, deployment: str, api_version: str,
                 credential: "TokenCredential", timeout_s: float = 300.0):
        self._url = (
            f"{endpoint.rstrip('/')}/openai/deployments/{deployment}"
            f"/chat/completions?api-version={api_version}"
        )
        self._credential = credential
        self._timeout = timeout_s

    def extract(self, system_prompt, user_parts, json_schema, schema_name,
                max_output_tokens) -> dict[str, Any]:
        body = {
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_parts},
            ],
            "max_completion_tokens": max_output_tokens,
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": schema_name, "strict": True, "schema": json_schema},
            },
        }
        with httpx.Client(timeout=self._timeout) as client:
            resp = client.post(self._url, headers=_bearer(self._credential), json=body)
        resp.raise_for_status()
        data = resp.json()
        content = data["choices"][0]["message"]["content"]
        return json.loads(content)


class AnthropicFoundryStructuredLLM(StructuredLLM):
    """Claude on Foundry via the Anthropic Messages API with forced tool use.

    Foundry's Azure-hosted Claude deployments expose /anthropic/v1/messages.
    We register a single tool whose input_schema is our extraction schema and
    force the model to call it — the returned tool_use input block is the
    structured result.
    """

    ANTHROPIC_VERSION = "2023-06-01"

    def __init__(self, endpoint: str, deployment: str, credential: "TokenCredential",
                 timeout_s: float = 300.0):
        self._url = f"{endpoint.rstrip('/')}/anthropic/v1/messages"
        self._model = deployment
        self._credential = credential
        self._timeout = timeout_s

    @staticmethod
    def _to_anthropic_parts(user_parts: list[dict[str, Any]]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for part in user_parts:
            if part.get("type") == "text":
                out.append({"type": "text", "text": part["text"]})
            elif part.get("type") == "image_url":
                url = part["image_url"]["url"]
                # data:image/png;base64,<payload>
                header, payload = url.split(",", 1)
                media_type = header.split(";")[0].removeprefix("data:")
                out.append({
                    "type": "image",
                    "source": {"type": "base64", "media_type": media_type, "data": payload},
                })
        return out

    def extract(self, system_prompt, user_parts, json_schema, schema_name,
                max_output_tokens) -> dict[str, Any]:
        body = {
            "model": self._model,
            "max_tokens": max_output_tokens,
            "system": system_prompt,
            "messages": [{"role": "user", "content": self._to_anthropic_parts(user_parts)}],
            "tools": [{
                "name": schema_name,
                "description": "Record the structured extraction result.",
                "input_schema": json_schema,
            }],
            "tool_choice": {"type": "tool", "name": schema_name},
        }
        headers = _bearer(self._credential) | {"anthropic-version": self.ANTHROPIC_VERSION}
        with httpx.Client(timeout=self._timeout) as client:
            resp = client.post(self._url, headers=headers, json=body)
        resp.raise_for_status()
        data = resp.json()
        for block in data.get("content", []):
            if block.get("type") == "tool_use" and block.get("name") == schema_name:
                return block["input"]
        raise ValueError("Model response contained no tool_use block with the extraction schema.")


def build_llm(provider: str, endpoint: str, deployment: str, api_version: str,
              credential: "TokenCredential") -> StructuredLLM:
    if provider == "azure_openai":
        return AzureOpenAIStructuredLLM(endpoint, deployment, api_version, credential)
    if provider == "anthropic_foundry":
        return AnthropicFoundryStructuredLLM(endpoint, deployment, credential)
    raise ValueError(f"Unknown llm_provider: {provider}")
