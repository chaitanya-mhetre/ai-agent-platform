from __future__ import annotations

import asyncio
import contextlib
from typing import Protocol

from redis.asyncio import Redis


class JobQueue(Protocol):
    async def enqueue(self, run_id: str) -> None: ...
    async def dequeue(self, timeout_s: float) -> str | None: ...


class InMemoryQueue:
    def __init__(self) -> None:
        self._q: asyncio.Queue[str] = asyncio.Queue()

    async def enqueue(self, run_id: str) -> None:
        self._q.put_nowait(run_id)

    async def dequeue(self, timeout_s: float) -> str | None:
        with contextlib.suppress(TimeoutError):
            async with asyncio.timeout(timeout_s):
                return await self._q.get()
        return None

    def qsize(self) -> int:
        return self._q.qsize()


class RedisQueue:
    """LPUSH/BRPOP list. Delivery is at-least-once in effect: the store's lease is
    what guarantees a run is executed by one worker at a time."""

    def __init__(self, redis: Redis, key: str = "agentplat:runs") -> None:
        self.redis = redis
        self.key = key

    async def enqueue(self, run_id: str) -> None:
        await self.redis.lpush(self.key, run_id)

    async def dequeue(self, timeout_s: float) -> str | None:
        item = await self.redis.brpop([self.key], timeout=max(1, int(timeout_s)))
        if item is None:
            return None
        value = item[1]
        return value.decode() if isinstance(value, bytes) else str(value)
