from __future__ import annotations

import asyncio
import random
from collections.abc import Awaitable, Callable, Sequence

from agentplat.messages import Message, ModelResponse, ToolSchema
from agentplat.providers.base import ModelProvider, ProviderError


class RetryingProvider:
    """Retries transient provider errors with exponential backoff + full jitter."""

    def __init__(
        self,
        inner: ModelProvider,
        *,
        max_attempts: int = 3,
        base_delay_s: float = 0.5,
        max_delay_s: float = 8.0,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.inner = inner
        self.name = inner.name
        self.max_attempts = max_attempts
        self.base_delay_s = base_delay_s
        self.max_delay_s = max_delay_s
        self._sleep = sleep
        self.last_attempts = 0

    async def complete(
        self, messages: Sequence[Message], tools: Sequence[ToolSchema], *, model: str | None = None
    ) -> ModelResponse:
        for attempt in range(1, self.max_attempts + 1):
            self.last_attempts = attempt
            try:
                return await self.inner.complete(messages, tools, model=model)
            except ProviderError as exc:
                if not exc.transient or attempt == self.max_attempts:
                    raise
                cap = min(self.max_delay_s, self.base_delay_s * 2 ** (attempt - 1))
                await self._sleep(random.uniform(0, cap))
        raise AssertionError("unreachable")
