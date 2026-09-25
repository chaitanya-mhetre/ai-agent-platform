"""LangGraph re-implementation of the approval flow, and parity with the hand-built runtime.

Skipped unless the optional group is installed: `uv sync --group comparison`.
"""

from collections import Counter
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("langgraph")

from langgraph.checkpoint.memory import InMemorySaver  # noqa: E402

from agentplat.approvals import decide  # noqa: E402
from agentplat.config import Settings  # noqa: E402
from agentplat.container import Container, build_container  # noqa: E402
from agentplat.providers.fake import ScriptedProvider, call, reply  # noqa: E402
from agentplat.state import RunStatus  # noqa: E402
from agentplat.store.models import Agent  # noqa: E402
from agentplat.store.sql import new_id  # noqa: E402
from agentplat.tools.builtin.actions import (  # noqa: E402
    CalendarCreateEvent,
    DeleteRecords,
    SendNotification,
)
from agentplat.tools.registry import ToolRegistry  # noqa: E402
from agentplat.tools.services import Services  # noqa: E402
from comparison.langgraph_flow import Principal, build_graph, resume, start  # noqa: E402

TOOLS = ["send_notification", "delete_records", "calendar_create_event"]
ALL_PERMS = frozenset({"notify:send", "records:delete", "calendar:write"})
EVENT = {"title": "sync", "start": "2026-10-01T10:00:00", "duration_minutes": 30}


def make(
    script: list[Any], perms: frozenset[str] = ALL_PERMS, allowed: list[str] = TOOLS
) -> tuple[Any, Services, Counter[str]]:
    services = Services()
    registry = ToolRegistry(
        [
            SendNotification(services.outbox),
            DeleteRecords(services.records),
            CalendarCreateEvent(services.calendar),
        ]
    )
    started: Counter[str] = Counter()
    graph = build_graph(
        ScriptedProvider(script),
        registry,
        principal=Principal("acme", "alice", perms),
        allowed_tools=allowed,
        checkpointer=InMemorySaver(),
        on_tool_start=lambda name: started.update([name]),
    )
    return graph, services, started


async def go(graph: Any) -> Any:
    return await start(graph, "t1", system_prompt="sys", user_input="do it")


async def test_destructive_tool_pauses_then_runs_once_after_approval() -> None:
    graph, services, started = make(
        [call("delete_records", table="orders", where="1=1"), reply("deleted")]
    )
    out = await go(graph)
    assert out.status == "awaiting_approval"
    (req,) = out.pending
    assert req.tool == "delete_records" and "always require approval" in req.reason
    assert services.records.tables["orders"], "must not run before approval"

    out = await resume(graph, "t1", decision="approve")
    assert out.status == "succeeded" and out.final_output == "deleted"
    assert services.records.tables["orders"] == []
    assert started["delete_records"] == 1


async def test_rejection_is_an_observation_and_the_run_continues() -> None:
    graph, services, _ = make(
        [call("send_notification", recipient="all", message="spam"), reply("ok, not sent")]
    )
    await go(graph)
    out = await resume(graph, "t1", decision="reject", comment="no spam")
    assert out.status == "succeeded"
    assert any("rejected by a human reviewer: no spam" in m.content for m in out.messages)
    assert not services.outbox.sent


async def test_missing_permission_is_denied_without_asking_a_human() -> None:
    graph, services, _ = make(
        [call("delete_records", table="orders", where="1=1"), reply("could not")],
        perms=frozenset(),
    )
    out = await go(graph)
    assert out.status == "succeeded" and not out.pending
    assert any("missing permissions" in m.content for m in out.messages)
    assert services.records.tables["orders"]


async def test_tool_not_on_allow_list_is_denied() -> None:
    graph, services, _ = make(
        [call("delete_records", table="orders", where="1=1"), reply("no")],
        allowed=["calendar_create_event"],
    )
    out = await go(graph)
    assert any("not allowed for this agent" in m.content for m in out.messages)
    assert services.records.tables["orders"]


async def test_extra_arguments_are_rejected() -> None:
    graph, services, _ = make(
        [call("send_notification", recipient="a", message="hi", bcc="x"), reply("no")]
    )
    out = await go(graph)
    assert not out.pending and any("invalid arguments" in m.content for m in out.messages)


async def test_side_effects_before_interrupt_rerun_on_resume() -> None:
    """The key LangGraph gotcha: resume re-executes the whole node from the top.

    One model turn proposes a calendar write (no approval) and a delete (approval).
    The calendar tool runs before the interrupt, then *again* when the node resumes.
    Only the idempotency key keeps the side effect single.
    """
    both = call("calendar_create_event", "c1", **EVENT)
    both.tool_calls.append(call("delete_records", "c2", table="orders", where="1=1").tool_calls[0])
    graph, services, started = make([both, reply("done")])

    await go(graph)
    assert started["calendar_create_event"] == 1
    await resume(graph, "t1", decision="approve")

    assert started["calendar_create_event"] == 2, "node re-ran from the top"
    assert len(services.calendar.events) == 1, "idempotency key deduped the repeat"
    assert started["delete_records"] == 1


# --- parity with the hand-built runtime ---------------------------------------


@pytest.fixture
async def c(tmp_path: Path) -> AsyncIterator[Container]:
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'p.db'}",
        embedded_worker=False,
        _env_file=None,
    )
    container = build_container(settings)
    await container.store.migrate()
    await container.store.grant("acme", "boss", ["approvals:decide"])
    yield container
    await container.close()


async def test_same_scenario_same_outcome_in_both_runtimes(c: Container) -> None:
    script = [call("delete_records", table="orders", where="1=1"), reply("deleted")]

    # hand-built
    agent = Agent(new_id(), "acme", "a", "sys", TOOLS)
    await c.store.create_agent(agent)
    await c.store.grant("acme", "alice", sorted(ALL_PERMS))
    provider = ScriptedProvider(list(script))
    c.provider_for = lambda _a: provider  # type: ignore[method-assign,assignment]
    run = await c.runtime.execute((await c.runtime.start_run(agent, "alice", "do it")).id)
    assert run.status is RunStatus.AWAITING_APPROVAL
    (pending,) = await c.store.list_approvals("acme", "pending")
    await decide(c, tenant_id="acme", user_id="boss", approval_id=pending.id, decision="approve")
    await c.worker().process_one()
    hand = await c.store.get_run(run.id)

    # LangGraph
    graph, services, _ = make(list(script))
    first = await go(graph)
    lg = await resume(graph, "t1", decision="approve")

    assert first.status == "awaiting_approval"
    assert hand.status is RunStatus.SUCCEEDED and lg.status == "succeeded"
    assert hand.final_output == lg.final_output == "deleted"
    assert c.services.records.tables["orders"] == services.records.tables["orders"] == []
