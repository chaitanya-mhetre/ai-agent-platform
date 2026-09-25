"""The durable agent loop: termination, persistence, leases, crash recovery, cancel, budgets."""

import asyncio
import json
from collections.abc import Callable
from typing import Any, ClassVar

import pytest

from agentplat.messages import Role
from agentplat.orchestrator import idempotency_key, unanswered_calls
from agentplat.providers.fake import ScriptedProvider, call, reply
from agentplat.state import IllegalTransitionError, RunStatus, check_transition
from agentplat.store.sql import SqlStore
from agentplat.tools.base import RiskLevel, Tool, ToolArgs, ToolContext, ToolResult
from agentplat.tools.builtin.calculator import Calculator
from agentplat.tools.registry import ToolRegistry
from tests.conftest import MakeRuntime


async def test_direct_answer(make_runtime: MakeRuntime, make_agent: Callable[..., Any]) -> None:
    agent = await make_agent()
    rt = make_runtime(ScriptedProvider([reply("hi")]))
    run = await rt.execute((await rt.start_run(agent, "u1", "hello")).id)
    assert run.status is RunStatus.SUCCEEDED
    assert run.final_output == "hi"
    assert run.step_count == 1


async def test_multi_step_tool_use_is_persisted(
    make_runtime: MakeRuntime, make_agent: Callable[..., Any], store: SqlStore
) -> None:
    agent = await make_agent()
    provider = ScriptedProvider(
        [
            call("calculator", expression="0.17 * 2340 + 12"),
            call("notes_write", key="answer", value="409.8"),
            reply("The answer is 409.8"),
        ]
    )
    rt = make_runtime(provider)
    run = await rt.execute((await rt.start_run(agent, "u1", "17% of 2340 + 12?")).id)
    assert run.status is RunStatus.SUCCEEDED

    stored = await store.get_run(run.id)
    tool_msgs = [m for m in stored.messages if m.role is Role.TOOL]
    assert json.loads(tool_msgs[0].content)["result"] == pytest.approx(409.8)
    assert await store.get("acme", "u1", "answer") == "409.8"
    assert [r.tool_name for r in await store.list_tool_calls(run.id)] == [
        "calculator",
        "notes_write",
    ]
    # the model saw the observation before answering
    assert any(m.role is Role.TOOL and m.name == "notes_write" for m in provider.calls[-1])
    assert stored.prompt_tokens == 30


async def test_max_steps(make_runtime: MakeRuntime, make_agent: Callable[..., Any]) -> None:
    agent = await make_agent(max_steps=3)
    rt = make_runtime(ScriptedProvider([call("calculator", expression="1+1")] * 5))
    run = await rt.execute((await rt.start_run(agent, "u1", "loop")).id)
    assert run.status is RunStatus.MAX_STEPS
    assert run.step_count == 3


async def test_token_budget(make_runtime: MakeRuntime, make_agent: Callable[..., Any]) -> None:
    agent = await make_agent(max_tokens=100)
    rt = make_runtime(ScriptedProvider([call("calculator", expression="1+1")] * 20))
    run = await rt.execute((await rt.start_run(agent, "u1", "x")).id)
    assert run.status is RunStatus.BUDGET_EXCEEDED
    assert run.prompt_tokens + run.completion_tokens >= 100


async def test_tool_not_on_agent_allow_list_is_denied(
    make_runtime: MakeRuntime, make_agent: Callable[..., Any], store: SqlStore
) -> None:
    agent = await make_agent(allowed_tools=["calculator"])
    rt = make_runtime(ScriptedProvider([call("notes_write", key="k", value="v"), reply("ok")]))
    run = await rt.execute((await rt.start_run(agent, "u1", "x")).id)
    tool_msg = next(m for m in run.messages if m.role is Role.TOOL)
    assert "not available" in tool_msg.content
    assert await store.get("acme", "u1", "k") is None
    (rec,) = await store.list_tool_calls(run.id)
    assert rec.decision == "denied" and rec.status == "denied"


async def test_invalid_args_reported_to_model(
    make_runtime: MakeRuntime, make_agent: Callable[..., Any]
) -> None:
    agent = await make_agent()
    rt = make_runtime(ScriptedProvider([call("calculator", expr="1+1"), reply("sorry")]))
    run = await rt.execute((await rt.start_run(agent, "u1", "x")).id)
    tool_msg = next(m for m in run.messages if m.role is Role.TOOL)
    assert "invalid arguments" in tool_msg.content


async def test_provider_error_fails_run(
    make_runtime: MakeRuntime, make_agent: Callable[..., Any]
) -> None:
    agent = await make_agent()
    rt = make_runtime(ScriptedProvider([RuntimeError("boom")]))
    run = await rt.execute((await rt.start_run(agent, "u1", "x")).id)
    assert run.status is RunStatus.FAILED
    assert run.error is not None and "boom" in run.error


async def test_cancel_requested(
    make_runtime: MakeRuntime, make_agent: Callable[..., Any], store: SqlStore
) -> None:
    agent = await make_agent()
    rt = make_runtime(ScriptedProvider([reply("never")]))
    run = await rt.start_run(agent, "u1", "x")
    await store.request_cancel(run.id)
    run = await rt.execute(run.id)
    assert run.status is RunStatus.CANCELLED


def test_state_machine() -> None:
    check_transition(RunStatus.QUEUED, RunStatus.RUNNING)
    check_transition(RunStatus.AWAITING_APPROVAL, RunStatus.QUEUED)
    for bad in [
        (RunStatus.SUCCEEDED, RunStatus.RUNNING),
        (RunStatus.QUEUED, RunStatus.SUCCEEDED),
        (RunStatus.AWAITING_APPROVAL, RunStatus.SUCCEEDED),
    ]:
        with pytest.raises(IllegalTransitionError):
            check_transition(*bad)


async def test_transition_is_compare_and_set(
    store: SqlStore, make_agent: Callable[..., Any]
) -> None:
    from agentplat.store.models import Run

    agent = await make_agent()
    run = Run("r1", "acme", agent.id, 1, "u", RunStatus.QUEUED, "x", [])
    await store.create_run(run)
    first, second = await asyncio.gather(
        store.transition("r1", RunStatus.QUEUED, RunStatus.RUNNING),
        store.transition("r1", RunStatus.QUEUED, RunStatus.RUNNING),
    )
    assert sorted([first, second]) == [False, True]


def test_idempotency_key_is_stable_and_arg_order_independent() -> None:
    from agentplat.messages import ToolCall

    a = ToolCall("c1", "t", {"x": 1, "y": 2})
    b = ToolCall("c1", "t", {"y": 2, "x": 1})
    assert idempotency_key("r", a) == idempotency_key("r", b)
    assert idempotency_key("r", a) != idempotency_key("r2", a)


def test_unanswered_calls() -> None:
    from agentplat.messages import Message, ToolCall

    msgs = [
        Message(Role.USER, "x"),
        Message(Role.ASSISTANT, "", tool_calls=[ToolCall("a", "t", {}), ToolCall("b", "t", {})]),
        Message(Role.TOOL, "{}", tool_call_id="a", name="t"),
    ]
    assert [c.id for c in unanswered_calls(msgs)] == ["b"]


# --- crash recovery -------------------------------------------------------------


class SimulatedCrash(BaseException):
    """Like a SIGKILL: not an Exception, so the runtime can't handle it."""


class Outbox:
    def __init__(self) -> None:
        self.sent: dict[str, str] = {}

    def send(self, key: str, message: str) -> None:
        self.sent.setdefault(key, message)  # the downstream service dedupes on the key


class SendArgs(ToolArgs):
    message: str


class CrashAfterSend(Tool[SendArgs]):
    name: ClassVar[str] = "send"
    description: ClassVar[str] = "send a message"
    args_model = SendArgs
    risk_level = RiskLevel.EXTERNAL

    def __init__(self, outbox: Outbox) -> None:
        self.outbox = outbox
        self.crash_next = True
        self.executions = 0

    async def run(self, args: SendArgs, ctx: ToolContext) -> ToolResult:
        self.executions += 1
        self.outbox.send(ctx.idempotency_key, args.message)
        if self.crash_next:
            self.crash_next = False
            raise SimulatedCrash  # side effect happened, result never recorded
        return ToolResult({"sent": True})


async def test_worker_crash_mid_tool_resumes_without_duplicate_side_effect(
    make_runtime: MakeRuntime, make_agent: Callable[..., Any], store: SqlStore
) -> None:
    outbox = Outbox()
    tool = CrashAfterSend(outbox)
    reg = ToolRegistry([Calculator(), tool])
    agent = await make_agent(allowed_tools=["send"])
    script = [call("send", message="hello"), reply("sent it")]

    worker_a = make_runtime(
        ScriptedProvider(list(script)), registry=reg, worker_id="a", lease_ttl_s=0.05
    )
    run = await worker_a.start_run(agent, "u1", "send hello")
    with pytest.raises(SimulatedCrash):
        await worker_a.execute(run.id)
    assert (await store.get_run(run.id)).status is RunStatus.RUNNING

    await asyncio.sleep(0.1)  # lease expires
    assert run.id in await store.reclaimable_runs()

    # worker B resumes from persisted state; the model is NOT asked to re-plan the send
    worker_b = make_runtime(ScriptedProvider([reply("sent it")]), registry=reg, worker_id="b")
    run = await worker_b.execute(run.id)
    assert run.status is RunStatus.SUCCEEDED
    assert tool.executions == 2  # retried after the crash...
    assert len(outbox.sent) == 1  # ...but the side effect happened once
    (rec,) = await store.list_tool_calls(run.id)
    assert rec.status == "succeeded"


async def test_only_lease_holder_drives_a_run(
    make_runtime: MakeRuntime, make_agent: Callable[..., Any]
) -> None:
    agent = await make_agent()
    gate = asyncio.Event()

    class SlowProvider(ScriptedProvider):
        async def complete(self, *a: Any, **k: Any) -> Any:
            await gate.wait()
            return await super().complete(*a, **k)

    p1 = SlowProvider([reply("from a")])
    p2 = SlowProvider([reply("from b")])
    a = make_runtime(p1, worker_id="a")
    b = make_runtime(p2, worker_id="b")
    run = await a.start_run(agent, "u1", "x")
    task_a = asyncio.create_task(a.execute(run.id))
    await asyncio.sleep(0.05)
    result_b = await b.execute(run.id)  # lease held by a -> returns immediately
    assert result_b.status is RunStatus.RUNNING
    gate.set()
    result_a = await task_a
    assert result_a.final_output == "from a"
    assert len(p2.calls) == 0
