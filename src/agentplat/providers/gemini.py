"""Google Gemini generateContent adapter (function calling)."""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

import httpx

from agentplat.messages import Message, ModelResponse, Role, ToolCall, ToolSchema, Usage
from agentplat.providers._http import post_json
from agentplat.providers.base import ProviderError

# Gemini accepts an OpenAPI subset of JSON Schema; these keys are rejected or ignored.
_UNSUPPORTED_SCHEMA_KEYS = {"additionalProperties", "title", "$defs", "$schema", "default"}


class GeminiProvider:
    name = "gemini"

    def __init__(
        self,
        api_key: str,
        model: str,
        *,
        base_url: str = "https://generativelanguage.googleapis.com/v1beta",
        client: httpx.AsyncClient | None = None,
        timeout_s: float = 60.0,
    ) -> None:
        self._key = api_key
        self.model = model
        self.base_url = base_url.rstrip("/")
        self._client = client or httpx.AsyncClient(timeout=timeout_s)

    async def complete(
        self, messages: Sequence[Message], tools: Sequence[ToolSchema], *, model: str | None = None
    ) -> ModelResponse:
        system, contents = to_gemini_contents(messages)
        body: dict[str, Any] = {"contents": contents}
        if system:
            body["systemInstruction"] = {"parts": [{"text": system}]}
        if tools:
            body["tools"] = [
                {
                    "functionDeclarations": [
                        {
                            "name": t.name,
                            "description": t.description,
                            "parameters": to_gemini_schema(t.parameters),
                        }
                        for t in tools
                    ]
                }
            ]
        data = await post_json(
            self._client,
            f"{self.base_url}/models/{model or self.model}:generateContent",
            json=body,
            headers={"x-goog-api-key": self._key},
        )
        return parse_gemini_response(data)


def to_gemini_schema(schema: Any) -> Any:
    if isinstance(schema, dict):
        return {
            k: to_gemini_schema(v) for k, v in schema.items() if k not in _UNSUPPORTED_SCHEMA_KEYS
        }
    if isinstance(schema, list):
        return [to_gemini_schema(v) for v in schema]
    return schema


def to_gemini_contents(messages: Sequence[Message]) -> tuple[str, list[dict[str, Any]]]:
    system_parts: list[str] = []
    contents: list[dict[str, Any]] = []
    for m in messages:
        if m.role is Role.SYSTEM:
            system_parts.append(m.content)
        elif m.role is Role.USER:
            contents.append({"role": "user", "parts": [{"text": m.content}]})
        elif m.role is Role.ASSISTANT:
            parts: list[dict[str, Any]] = []
            if m.content:
                parts.append({"text": m.content})
            parts += [{"functionCall": {"name": c.name, "args": c.arguments}} for c in m.tool_calls]
            contents.append({"role": "model", "parts": parts})
        else:
            try:
                payload = json.loads(m.content)
            except json.JSONDecodeError:
                payload = m.content
            part = {"functionResponse": {"name": m.name, "response": {"result": payload}}}
            last = contents[-1] if contents else None
            if last and last["role"] == "user" and "functionResponse" in last["parts"][0]:
                last["parts"].append(part)
            else:
                contents.append({"role": "user", "parts": [part]})
    return "\n\n".join(system_parts), contents


def parse_gemini_response(data: dict[str, Any]) -> ModelResponse:
    try:
        cand = data["candidates"][0]
    except (KeyError, IndexError) as exc:
        block = (data.get("promptFeedback") or {}).get("blockReason")
        raise ProviderError(f"no candidates (blockReason={block})") from exc
    text: list[str] = []
    calls: list[ToolCall] = []
    for i, part in enumerate((cand.get("content") or {}).get("parts") or []):
        if "text" in part:
            text.append(part["text"])
        elif "functionCall" in part:
            fc = part["functionCall"]
            calls.append(ToolCall(fc.get("id") or f"gemini_{i}", fc["name"], fc.get("args") or {}))
    usage = data.get("usageMetadata") or {}
    return ModelResponse(
        content="".join(text) or None,
        tool_calls=calls,
        usage=Usage(usage.get("promptTokenCount", 0), usage.get("candidatesTokenCount", 0)),
        model=data.get("modelVersion", ""),
        finish_reason=cand.get("finishReason") or "STOP",
    )
