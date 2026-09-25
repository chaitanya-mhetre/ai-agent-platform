"""Composition root: builds and wires every component from Settings."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from redis.asyncio import Redis

from agentplat.config import Settings
from agentplat.guards import GuardPipeline
from agentplat.observability.pricing import PriceTable
from agentplat.observability.tracing import Tracer, configure_otel
from agentplat.orchestrator import Runtime
from agentplat.providers.base import ModelProvider
from agentplat.providers.factory import build_provider
from agentplat.runtime.events import EventBus, EventPublisher, InMemoryBus, RedisBus
from agentplat.runtime.queue import InMemoryQueue, JobQueue, RedisQueue
from agentplat.runtime.worker import Worker
from agentplat.security.ratelimit import InMemoryRateLimiter, RateLimiter, RedisRateLimiter
from agentplat.security.redaction import Redactor, SecretStore
from agentplat.store.models import Agent
from agentplat.store.sql import SqlStore
from agentplat.tools.builtin.actions import CalendarCreateEvent, DeleteRecords, SendNotification
from agentplat.tools.builtin.calculator import Calculator
from agentplat.tools.builtin.data import FileAnalyze, SqlQuery
from agentplat.tools.builtin.notes import NotesRead, NotesWrite
from agentplat.tools.builtin.web import HttpGet, WebSearch
from agentplat.tools.fixture_web import fixture_resolver_for, fixture_transport
from agentplat.tools.registry import ToolRegistry
from agentplat.tools.services import Services


def default_registry(store: SqlStore, services: Services, settings: Settings) -> ToolRegistry:
    data = Path(settings.data_dir)
    http_get = (
        HttpGet(
            resolver=fixture_resolver_for(data / "pages"),
            transport=fixture_transport(data / "pages"),
        )
        if settings.offline_web
        else HttpGet()
    )
    return ToolRegistry(
        [
            Calculator(),
            NotesWrite(store),
            NotesRead(store),
            http_get,
            WebSearch(data / "search" / "index.json"),
            FileAnalyze(data / "files"),
            SqlQuery(data / "sample.db"),
            SendNotification(services.outbox),
            CalendarCreateEvent(services.calendar),
            DeleteRecords(services.records),
        ]
    )


@dataclass
class Container:
    settings: Settings
    store: SqlStore
    bus: EventBus
    queue: JobQueue
    registry: ToolRegistry
    runtime: Runtime
    services: Services
    limiter: RateLimiter
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

    async def bootstrap(self) -> None:
        """Dev convenience: grant `admin` to tenant:user pairs from settings."""
        for entry in self.settings.bootstrap_admins:
            tenant, _, user = entry.partition(":")
            if tenant and user:
                await self.store.grant(tenant, user, ["admin", "approvals:decide"])

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
    services = Services()
    registry = registry or default_registry(store, services, settings)
    limiter: RateLimiter = RedisRateLimiter(redis) if redis else InMemoryRateLimiter()
    secrets = SecretStore(settings.tool_secrets)
    provider_keys = [
        k for k in (settings.api_key_for(p) for p in ("openai", "anthropic", "gemini")) if k
    ]
    redactor = Redactor([*secrets.all_values(), *provider_keys])
    configure_otel(settings.otel_endpoint)
    holder: dict[str, Container] = {}
    runtime = Runtime(
        store,
        registry,
        lambda agent: holder["c"].provider_for(agent),
        EventPublisher(store, bus, redactor),
        worker_id=worker_id,
        lease_ttl_s=settings.lease_ttl_s,
        guard=GuardPipeline(store.permissions, store.approval_for_call),
        secrets=secrets,
        redactor=redactor,
        tracer=Tracer(store, redactor),
        prices=PriceTable.load(settings.pricing_file),
    )
    c = Container(settings, store, bus, queue, registry, runtime, services, limiter, redis)
    holder["c"] = c
    return c
