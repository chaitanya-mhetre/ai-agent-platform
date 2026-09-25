"""Run events: persisted (for replay) and published (for live SSE tailing)."""

from __future__ import annotations

import asyncio
import json
from collections import defaultdict
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, Protocol

from redis.asyncio import Redis

from agentplat.security.redaction import Redactor
from agentplat.store.sql import SqlStore

Event = dict[str, Any]


class EventBus(Protocol):
    async def publish(self, run_id: str, event: Event) -> None: ...

    def subscribe(self, run_id: str) -> Any:
        """Async context manager yielding an async iterator of events."""
        ...


class InMemoryBus:
    def __init__(self) -> None:
        self._subs: dict[str, set[asyncio.Queue[Event]]] = defaultdict(set)

    async def publish(self, run_id: str, event: Event) -> None:
        for q in list(self._subs[run_id]):
            q.put_nowait(event)

    @asynccontextmanager
    async def subscribe(self, run_id: str) -> AsyncIterator[AsyncIterator[Event]]:
        q: asyncio.Queue[Event] = asyncio.Queue()
        self._subs[run_id].add(q)

        async def gen() -> AsyncIterator[Event]:
            while True:
                yield await q.get()

        try:
            yield gen()
        finally:
            self._subs[run_id].discard(q)


class RedisBus:
    def __init__(self, redis: Redis) -> None:
        self.redis = redis

    async def publish(self, run_id: str, event: Event) -> None:
        await self.redis.publish(f"agentplat:run:{run_id}", json.dumps(event, default=str))

    @asynccontextmanager
    async def subscribe(self, run_id: str) -> AsyncIterator[AsyncIterator[Event]]:
        pubsub = self.redis.pubsub()
        await pubsub.subscribe(f"agentplat:run:{run_id}")

        async def gen() -> AsyncIterator[Event]:
            while True:
                msg = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
                if msg is not None:
                    data: Event = json.loads(msg["data"])
                    yield data

        try:
            yield gen()
        finally:
            await pubsub.unsubscribe()
            await pubsub.aclose()  # type: ignore[no-untyped-call]


class EventPublisher:
    def __init__(self, store: SqlStore, bus: EventBus, redactor: Redactor | None = None) -> None:
        self.store = store
        self.bus = bus
        self.redactor = redactor or Redactor()

    async def emit(self, run_id: str, type_: str, **data: Any) -> None:
        data = self.redactor.deep(data)
        seq = await self.store.append_event(run_id, type_, data)
        await self.bus.publish(run_id, {"seq": seq, "type": type_, "data": data})
