"""OpenAI Chat Completions adapter (function calling). Also works with any
OpenAI-compatible endpoint (e.g. a local gateway) via `base_url`."""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

import httpx

from agentplat.messages import Message, ModelResponse, Role, ToolCall, ToolSchema, Usage
from agentplat.providers._http import post_json
from agentplat.providers.base import ProviderError


class OpenAIProvider:
    name = "openai"

    def __init__(
        self,
        api_key: str,
        model: str,
        *,
        base_url: str = "https://api.openai.com/v1",
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
        body: dict[str, Any] = {
            "model": model or self.model,
            "messages": [to_openai_message(m) for m in messages],
        }
        if tools:
            body["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": t.name,
                        "description": t.description,
                        "parameters": t.parameters,
                    },
                }
                for t in tools
            ]
        data = await post_json(
            self._client,
            f"{self.base_url}/chat/completions",
            json=body,
            headers={"Authorization": f"Bearer {self._key}"},
        )
        return parse_openai_response(data)


def to_openai_message(m: Message) -> dict[str, Any]:
    if m.role is Role.TOOL:
        return {"role": "tool", "tool_call_id": m.tool_call_id, "content": m.content}
    out: dict[str, Any] = {"role": m.role.value, "content": m.content or None}
    if m.tool_calls:
        out["tool_calls"] = [
            {
                "id": c.id,
                "type": "function",
                "function": {"name": c.name, "arguments": json.dumps(c.arguments)},
            }
            for c in m.tool_calls
        ]
    return out


def parse_openai_response(data: dict[str, Any]) -> ModelResponse:
    try:
        choice = data["choices"][0]
        msg = choice["message"]
    except (KeyError, IndexError) as exc:
        raise ProviderError(f"malformed response: {str(data)[:200]}") from exc
    calls: list[ToolCall] = []
    for tc in msg.get("tool_calls") or []:
        raw = tc["function"].get("arguments") or "{}"
        try:
            args = json.loads(raw)
        except json.JSONDecodeError:
            # Surface as an argument the validator will reject, so the model
            # gets an "invalid arguments" observation and can retry.
            args = {"__unparseable_arguments__": raw}
        calls.append(ToolCall(tc["id"], tc["function"]["name"], args))
    usage = data.get("usage") or {}
    return ModelResponse(
        content=msg.get("content"),
        tool_calls=calls,
        usage=Usage(usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0)),
        model=data.get("model", ""),
        finish_reason=choice.get("finish_reason") or "stop",
    )
