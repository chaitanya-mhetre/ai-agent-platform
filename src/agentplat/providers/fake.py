"""Deterministic fake providers for tests, local dev and offline evaluation."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

from agentplat.messages import Message, ModelResponse, ToolCall, ToolSchema, Usage


class ScriptExhaustedError(Exception):
    pass


Step = ModelResponse | Exception | Callable[[Sequence[Message]], ModelResponse]


def reply(text: str, *, prompt_tokens: int = 10, completion_tokens: int = 5) -> ModelResponse:
    return ModelResponse(text, [], Usage(prompt_tokens, completion_tokens), model="scripted")


def call(
    name: str,
    call_id: str | None = None,
    /,
    **arguments: object,
) -> ModelResponse:
    return ModelResponse(
        None,
        [ToolCall(call_id or f"call_{name}", name, dict(arguments))],
        Usage(10, 5),
        model="scripted",
        finish_reason="tool_calls",
    )


@dataclass
class ScriptedProvider:
    """Returns pre-programmed responses in order.

    A step may be a ModelResponse, an Exception (raised), or a callable that
    receives the conversation so far and builds a response from it.
    """

    steps: list[Step]
    name: str = "scripted"
    calls: list[list[Message]] = field(default_factory=list)

    async def complete(
        self,
        messages: Sequence[Message],
        tools: Sequence[ToolSchema],
        *,
        model: str | None = None,
    ) -> ModelResponse:
        self.calls.append(list(messages))
        if not self.steps:
            raise ScriptExhaustedError("scripted provider has no more steps")
        step = self.steps.pop(0)
        if isinstance(step, Exception):
            raise step
        if isinstance(step, ModelResponse):
            return step
        return step(messages)
