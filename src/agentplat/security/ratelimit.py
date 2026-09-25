"""Per-user / per-tenant request limits (fixed window)."""

from __future__ import annotations

import time
from collections import defaultdict
from typing import Protocol

from redis.asyncio import Redis


class RateLimiter(Protocol):
    async def hit(self, key: str, limit: int, window_s: int = 60) -> tuple[bool, int]:
        """Returns (allowed, retry_after_seconds)."""
        ...


class InMemoryRateLimiter:
    def __init__(self) -> None:
        self._counts: dict[tuple[str, int], int] = defaultdict(int)

    async def hit(self, key: str, limit: int, window_s: int = 60) -> tuple[bool, int]:
        now = int(time.time())
        window = now // window_s
        self._counts[(key, window)] += 1
        if self._counts[(key, window)] > limit:
            return False, window_s - (now % window_s)
        return True, 0


class RedisRateLimiter:
    def __init__(self, redis: Redis) -> None:
        self.redis = redis

    async def hit(self, key: str, limit: int, window_s: int = 60) -> tuple[bool, int]:
        now = int(time.time())
        bucket = f"agentplat:rl:{key}:{now // window_s}"
        async with self.redis.pipeline(transaction=True) as pipe:
            pipe.incr(bucket)
            pipe.expire(bucket, window_s + 1)
            count, _ = await pipe.execute()
        if int(count) > limit:
            return False, window_s - (now % window_s)
        return True, 0
