"""Anthropic Messages API adapter (tool use)."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import httpx

from agentplat.messages import Message, ModelResponse, Role, ToolCall, ToolSchema, Usage
from agentplat.providers._http import post_json
from agentplat.providers.base import ProviderError

API_VERSION = "2023-06-01"


class AnthropicProvider:
    name = "anthropic"

    def __init__(
        self,
        api_key: str,
        model: str,
        *,
        max_tokens: int = 2048,
        base_url: str = "https://api.anthropic.com/v1",
        client: httpx.AsyncClient | None = None,
        timeout_s: float = 60.0,
    ) -> None:
        self._key = api_key
        self.model = model
        self.max_tokens = max_tokens
        self.base_url = base_url.rstrip("/")
        self._client = client or httpx.AsyncClient(timeout=timeout_s)

    async def complete(
        self, messages: Sequence[Message], tools: Sequence[ToolSchema], *, model: str | None = None
    ) -> ModelResponse:
        system, turns = to_anthropic_messages(messages)
        body: dict[str, Any] = {
            "model": model or self.model,
            "max_tokens": self.max_tokens,
            "messages": turns,
        }
        if system:
            body["system"] = system
        if tools:
            body["tools"] = [
                {"name": t.name, "description": t.description, "input_schema": t.parameters}
                for t in tools
            ]
        data = await post_json(
            self._client,
            f"{self.base_url}/messages",
            json=body,
            headers={"x-api-key": self._key, "anthropic-version": API_VERSION},
        )
        return parse_anthropic_response(data)


def to_anthropic_messages(messages: Sequence[Message]) -> tuple[str, list[dict[str, Any]]]:
    """System prompt goes top-level; tool results are user-role `tool_result` blocks,
    and consecutive results must share one user turn."""
    system_parts: list[str] = []
    turns: list[dict[str, Any]] = []
    for m in messages:
        if m.role is Role.SYSTEM:
            system_parts.append(m.content)
        elif m.role is Role.USER:
            turns.append({"role": "user", "content": m.content})
        elif m.role is Role.ASSISTANT:
            blocks: list[dict[str, Any]] = []
            if m.content:
                blocks.append({"type": "text", "text": m.content})
            blocks += [
                {"type": "tool_use", "id": c.id, "name": c.name, "input": c.arguments}
                for c in m.tool_calls
            ]
            turns.append({"role": "assistant", "content": blocks})
        else:
            block = {"type": "tool_result", "tool_use_id": m.tool_call_id, "content": m.content}
            last = turns[-1] if turns else None
            if last and last["role"] == "user" and isinstance(last["content"], list):
                last["content"].append(block)
            else:
                turns.append({"role": "user", "content": [block]})
    return "\n\n".join(system_parts), turns


def parse_anthropic_response(data: dict[str, Any]) -> ModelResponse:
    if "content" not in data:
        raise ProviderError(f"malformed response: {str(data)[:200]}")
    text: list[str] = []
    calls: list[ToolCall] = []
    for block in data["content"]:
        if block.get("type") == "text":
            text.append(block["text"])
        elif block.get("type") == "tool_use":
            calls.append(ToolCall(block["id"], block["name"], block.get("input") or {}))
    usage = data.get("usage") or {}
    return ModelResponse(
        content="".join(text) or None,
        tool_calls=calls,
        usage=Usage(usage.get("input_tokens", 0), usage.get("output_tokens", 0)),
        model=data.get("model", ""),
        finish_reason=data.get("stop_reason") or "end_turn",
    )
