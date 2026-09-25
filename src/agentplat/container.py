"""Composition root: builds and wires every component from Settings."""

from __future__ import annotations

from dataclasses import dataclass, field

from redis.asyncio import Redis

from agentplat.config import Settings
from agentplat.orchestrator import Runtime
from agentplat.providers.base import ModelProvider
from agentplat.providers.factory import build_provider
from agentplat.runtime.events import EventBus, EventPublisher, InMemoryBus, RedisBus
from agentplat.runtime.queue import InMemoryQueue, JobQueue, RedisQueue
from agentplat.runtime.worker import Worker
from agentplat.store.models import Agent
from agentplat.store.sql import SqlStore
from agentplat.tools.builtin.calculator import Calculator
from agentplat.tools.builtin.notes import NotesRead, NotesWrite
from agentplat.tools.registry import ToolRegistry


def default_registry(store: SqlStore) -> ToolRegistry:
    return ToolRegistry([Calculator(), NotesWrite(store), NotesRead(store)])


@dataclass
class Container:
    settings: Settings
    store: SqlStore
    bus: EventBus
    queue: JobQueue
    registry: ToolRegistry
    runtime: Runtime
    redis: Redis | None = None
    _providers: dict[tuple[str, str | None], ModelProvider] = field(default_factory=dict)

    def provider_for(self, agent: Agent) -> ModelProvider:
        kind = str(agent.model_config.get("provider") or self.settings.provider)
        model = agent.model_config.get("model") or self.settings.model
        key = (kind, model)
        if key not in self._providers:
            self._providers[key] = build_provider(
                kind,
                model=model,
                api_key=self.settings.api_key_for(kind),
                base_url=self.settings.openai_base_url,
            )
        return self._providers[key]

    def worker(self) -> Worker:
        return Worker(self.runtime, self.queue, concurrency=self.settings.worker_concurrency)

    async def close(self) -> None:
        await self.store.close()
        if self.redis is not None:
            await self.redis.aclose()


def build_container(
    settings: Settings | None = None,
    *,
    store: SqlStore | None = None,
    registry: ToolRegistry | None = None,
    worker_id: str = "worker-local",
) -> Container:
    settings = settings or Settings()
    store = store or SqlStore.from_url(settings.database_url)
    redis: Redis | None = None
    bus: EventBus
    queue: JobQueue
    if settings.redis_url:
        redis = Redis.from_url(settings.redis_url)
        bus, queue = RedisBus(redis), RedisQueue(redis)
    else:
        bus, queue = InMemoryBus(), InMemoryQueue()
    registry = registry or default_registry(store)
    holder: dict[str, Container] = {}
    runtime = Runtime(
        store,
        registry,
        lambda agent: holder["c"].provider_for(agent),
        EventPublisher(store, bus),
        worker_id=worker_id,
        lease_ttl_s=settings.lease_ttl_s,
    )
    c = Container(settings, store, bus, queue, registry, runtime, redis)
    holder["c"] = c
    return c
