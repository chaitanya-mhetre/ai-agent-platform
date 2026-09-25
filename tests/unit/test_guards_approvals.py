"""M4: execution-time authorization, policy, human approval, audit."""

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from agentplat.approvals import ApprovalError, decide
from agentplat.config import Settings
from agentplat.container import Container, build_container
from agentplat.messages import Role
from agentplat.providers.fake import ScriptedProvider, call, reply
from agentplat.state import RunStatus
from agentplat.store.models import Agent
from agentplat.store.sql import new_id

ALL_TOOLS = ["calculator", "notes_write", "send_notification", "delete_records"]


@pytest.fixture
async def c(tmp_path: Path) -> AsyncIterator[Container]:
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'g.db'}",
        embedded_worker=False,
        _env_file=None,
    )
    container = build_container(settings)
    await container.store.migrate()
    await container.store.grant("acme", "boss", ["approvals:decide"])
    yield container
    await container.close()


async def setup(
    c: Container, script: list[Any], perms: list[str], tools: list[str] | None = None
) -> tuple[Agent, str]:
    agent = Agent(new_id(), "acme", "a", "sys", tools or ALL_TOOLS)
    await c.store.create_agent(agent)
    if perms:
        await c.store.grant("acme", "alice", perms)
    provider = ScriptedProvider(script)
    c.provider_for = lambda _a: provider  # type: ignore[method-assign,assignment]
    run = await c.runtime.start_run(agent, "alice", "do it")
    return agent, run.id


def tool_obs(messages: list[Any]) -> list[str]:
    return [m.content for m in messages if m.role is Role.TOOL]


async def test_missing_permission_denies_and_never_executes(c: Container) -> None:
    _, run_id = await setup(c, [call("notes_write", key="k", value="v"), reply("ok")], perms=[])
    run = await c.runtime.execute(run_id)
    assert run.status is RunStatus.SUCCEEDED
    assert "permission denied: user lacks notes:write" in tool_obs(run.messages)[0]
    assert await c.store.get("acme", "alice", "k") is None


async def test_permission_granted_executes(c: Container) -> None:
    _, run_id = await setup(
        c, [call("notes_write", key="k", value="v"), reply("ok")], perms=["notes:write"]
    )
    await c.runtime.execute(run_id)
    assert await c.store.get("acme", "alice", "k") == "v"


@pytest.mark.parametrize(
    ("tool_call", "perms", "expect_status", "expect_in_obs"),
    [
        (call("calculator", expression="1+1"), [], RunStatus.SUCCEEDED, '"result"'),
        (call("notes_write", key="a", value="b"), [], RunStatus.SUCCEEDED, "permission denied"),
        (
            call("send_notification", recipient="x", message="m"),
            [],
            RunStatus.SUCCEEDED,
            "lacks notify:send",
        ),
        (
            call("send_notification", recipient="x", message="m"),
            ["notify:send"],
            RunStatus.AWAITING_APPROVAL,
            None,
        ),
        (
            call("delete_records", table="orders", where="1=1"),
            [],
            RunStatus.SUCCEEDED,
            "lacks records:delete",
        ),
        (
            call("delete_records", table="orders", where="1=1"),
            ["records:delete"],
            RunStatus.AWAITING_APPROVAL,
            None,
        ),
    ],
)
async def test_authorization_matrix(
    c: Container,
    tool_call: Any,
    perms: list[str],
    expect_status: RunStatus,
    expect_in_obs: str | None,
) -> None:
    _, run_id = await setup(c, [tool_call, reply("done")], perms)
    run = await c.runtime.execute(run_id)
    assert run.status is expect_status
    if expect_in_obs:
        assert expect_in_obs in tool_obs(run.messages)[0]
    assert c.services.records.tables["orders"], "destructive tool must never run unapproved"
    assert not c.services.outbox.sent


async def test_destructive_tool_waits_then_runs_once_after_approval(c: Container) -> None:
    _, run_id = await setup(
        c,
        [call("delete_records", table="orders", where="1=1"), reply("deleted")],
        perms=["records:delete"],
    )
    run = await c.runtime.execute(run_id)
    assert run.status is RunStatus.AWAITING_APPROVAL
    (pending,) = await c.store.list_approvals("acme", "pending")
    assert pending.tool_name == "delete_records"
    assert "always require approval" in pending.reason

    # executing again while paused does nothing
    assert (await c.runtime.execute(run_id)).status is RunStatus.AWAITING_APPROVAL

    await decide(c, tenant_id="acme", user_id="boss", approval_id=pending.id, decision="approve")
    assert (await c.store.get_run(run_id)).status is RunStatus.QUEUED
    assert await c.worker().process_one() == run_id

    run = await c.store.get_run(run_id)
    assert run.status is RunStatus.SUCCEEDED
    assert c.services.records.tables["orders"] == []
    (rec,) = await c.store.list_tool_calls(run_id)
    assert rec.status == "succeeded" and rec.attempts == 1


async def test_rejection_is_an_observation_and_the_run_continues(c: Container) -> None:
    _, run_id = await setup(
        c,
        [call("send_notification", recipient="all", message="spam"), reply("ok, not sent")],
        perms=["notify:send"],
    )
    await c.runtime.execute(run_id)
    (a,) = await c.store.list_approvals("acme", "pending")
    await decide(
        c, tenant_id="acme", user_id="boss", approval_id=a.id, decision="reject", comment="no spam"
    )
    await c.worker().process_one()
    run = await c.store.get_run(run_id)
    assert run.status is RunStatus.SUCCEEDED
    assert "rejected by a human reviewer: no spam" in tool_obs(run.messages)[0]
    assert not c.services.outbox.sent


async def test_edited_args_are_revalidated_then_used(c: Container) -> None:
    _, run_id = await setup(
        c,
        [call("send_notification", recipient="everyone", message="hi"), reply("sent")],
        perms=["notify:send"],
    )
    await c.runtime.execute(run_id)
    (a,) = await c.store.list_approvals("acme", "pending")
    await decide(
        c,
        tenant_id="acme",
        user_id="boss",
        approval_id=a.id,
        decision="edit",
        edited_args={"recipient": "team-lead", "message": "hi"},
    )
    await c.worker().process_one()
    (sent,) = c.services.outbox.sent.values()
    assert sent["recipient"] == "team-lead"
    run = await c.store.get_run(run_id)
    assert "edited the arguments" in tool_obs(run.messages)[0]


async def test_invalid_edited_args_rejected_by_schema(c: Container) -> None:
    _, run_id = await setup(
        c,
        [call("send_notification", recipient="x", message="hi"), reply("hm")],
        perms=["notify:send"],
    )
    await c.runtime.execute(run_id)
    (a,) = await c.store.list_approvals("acme", "pending")
    await decide(
        c,
        tenant_id="acme",
        user_id="boss",
        approval_id=a.id,
        decision="edit",
        edited_args={"recipient": "x", "message": "hi", "bcc": "attacker@evil.test"},
    )
    await c.worker().process_one()
    run = await c.store.get_run(run_id)
    assert "invalid arguments" in tool_obs(run.messages)[0]
    assert not c.services.outbox.sent


async def test_only_authorized_reviewers_decide_and_only_once(c: Container) -> None:
    _, run_id = await setup(
        c, [call("send_notification", recipient="x", message="m")], perms=["notify:send"]
    )
    await c.runtime.execute(run_id)
    (a,) = await c.store.list_approvals("acme", "pending")
    with pytest.raises(ApprovalError) as ei:
        await decide(c, tenant_id="acme", user_id="alice", approval_id=a.id, decision="approve")
    assert ei.value.status == 403
    await decide(c, tenant_id="acme", user_id="boss", approval_id=a.id, decision="approve")
    with pytest.raises(ApprovalError) as ei:
        await decide(c, tenant_id="acme", user_id="boss", approval_id=a.id, decision="reject")
    assert ei.value.status == 409


async def test_repeated_identical_calls_are_blocked(c: Container) -> None:
    same = call("calculator", expression="1/0")
    _, run_id = await setup(c, [same, same, same, same, reply("giving up")], perms=[])
    run = await c.runtime.execute(run_id)
    assert "repeated identical call blocked" in tool_obs(run.messages)[-1]


async def test_every_decision_is_audited(c: Container) -> None:
    _, run_id = await setup(
        c,
        [
            call("notes_write", key="k", value="v"),
            call("delete_records", table="orders", where="1=1"),
        ],
        perms=["records:delete"],
    )
    await c.runtime.execute(run_id)
    actions = [e["action"] for e in await c.store.list_audit("acme")]
    assert "tool.denied" in actions and "tool.approval_requested" in actions
