"""Same guarantees, real infrastructure. Run with `make integration`."""

import asyncio
import os
from collections.abc import AsyncIterator
from typing import Any

import pytest
from redis.asyncio import Redis

from agentplat.orchestrator import Runtime
from agentplat.providers.fake import ScriptedProvider, call, reply
from agentplat.runtime.events import EventPublisher, RedisBus
from agentplat.runtime.queue import RedisQueue
from agentplat.state import RunStatus
from agentplat.store.models import Agent, Run
from agentplat.store.sql import SqlStore, new_id
from agentplat.tools.builtin.calculator import Calculator
from agentplat.tools.registry import ToolRegistry

PG = os.environ.get("AGENTPLAT_TEST_PG_URL")
REDIS = os.environ.get("AGENTPLAT_TEST_REDIS_URL")

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not (PG and REDIS), reason="set AGENTPLAT_TEST_PG_URL/REDIS_URL (make integration)"
    ),
]


@pytest.fixture
async def pg() -> AsyncIterator[SqlStore]:
    assert PG
    s = SqlStore.from_url(PG)
    await s.drop_schema()
    await s.migrate()
    yield s
    await s.close()


@pytest.fixture
async def redis() -> AsyncIterator[Redis]:
    assert REDIS
    r = Redis.from_url(REDIS)
    await r.flushdb()
    yield r
    await r.aclose()


async def _agent(store: SqlStore) -> Agent:
    agent = Agent(new_id(), "acme", "a", "sys", ["calculator"])
    await store.create_agent(agent)
    return agent


async def test_full_run_on_postgres_with_redis_events(pg: SqlStore, redis: Redis) -> None:
    agent = await _agent(pg)
    bus = RedisBus(redis)
    rt = Runtime(
        pg,
        ToolRegistry([Calculator()]),
        lambda _a: ScriptedProvider([call("calculator", expression="6*7"), reply("42")]),
        EventPublisher(pg, bus),
    )
    run = await rt.start_run(agent, "u", "6*7")
    received: list[dict[str, Any]] = []

    async def listen() -> None:
        async with bus.subscribe(run.id) as live:
            async for ev in live:
                received.append(ev)
                if ev["type"] == "status" and ev["data"]["status"] == "succeeded":
                    return

    listener = asyncio.create_task(listen())
    await asyncio.sleep(0.2)
    result = await rt.execute(run.id)
    await asyncio.wait_for(listener, 5)
    assert result.status is RunStatus.SUCCEEDED
    assert any(e["type"] == "tool_result" for e in received)
    assert len(await pg.list_events(run.id)) >= len(received)


async def test_status_cas_under_contention(pg: SqlStore) -> None:
    agent = await _agent(pg)
    await pg.create_run(Run("r1", "acme", agent.id, 1, "u", RunStatus.QUEUED, "x", []))
    results = await asyncio.gather(
        *[pg.transition("r1", RunStatus.QUEUED, RunStatus.RUNNING) for _ in range(10)]
    )
    assert results.count(True) == 1


async def test_lease_exclusive_under_contention(pg: SqlStore) -> None:
    agent = await _agent(pg)
    await pg.create_run(Run("r2", "acme", agent.id, 1, "u", RunStatus.QUEUED, "x", []))
    results = await asyncio.gather(*[pg.acquire_lease("r2", f"w{i}", 30) for i in range(10)])
    assert results.count(True) == 1


async def test_redis_queue_roundtrip(redis: Redis) -> None:
    q = RedisQueue(redis, key="test:q")
    await q.enqueue("a")
    await q.enqueue("b")
    assert [await q.dequeue(1), await q.dequeue(1)] == ["a", "b"]
    assert await q.dequeue(1) is None


async def test_concurrent_migrations_are_serialized_by_advisory_lock(pg: SqlStore) -> None:
    """API and worker starting together must not race on DDL."""
    assert PG
    other = SqlStore.from_url(PG)
    try:
        await pg.downgrade("base")
        await asyncio.gather(pg.migrate(), other.migrate(), pg.migrate())
        assert await pg.schema_revision() == "0001"
    finally:
        await other.close()
