from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol, runtime_checkable

from agentplat.messages import Message, ModelResponse, ToolSchema


class ProviderError(Exception):
    """A model call failed. `transient` errors (timeouts, 429, 5xx) may be retried."""

    def __init__(self, message: str, *, transient: bool = False, status: int | None = None):
        super().__init__(message)
        self.transient = transient
        self.status = status


@runtime_checkable
class ModelProvider(Protocol):
    """Anything that can turn a conversation + tool list into a ModelResponse."""

    name: str

    async def complete(
        self,
        messages: Sequence[Message],
        tools: Sequence[ToolSchema],
        *,
        model: str | None = None,
    ) -> ModelResponse: ...
