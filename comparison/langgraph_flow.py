"""The "risky tools need a human" flow, re-implemented on LangGraph.

Same model providers, tools and fake services as the hand-built runtime
(`agentplat.orchestrator.Runtime`), so the comparison isolates *orchestration*:
the agent loop, persistence, the approval pause and resume.

    START -> agent --(tool calls?)--> tools -> agent ... -> END
                 \\--(final answer / step budget)--> END

What LangGraph gives for free: the graph, per-step checkpoints (thread_id),
`interrupt()` + `Command(resume=...)` for the human pause, streaming.
What still has to be written by hand (it's all in `_tools_node`): schema
validation, allow-list x user-permission authorization, the risk policy,
idempotency keys, timeouts. See docs/framework-comparison.md for the findings.

Install with: uv sync --group comparison
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import operator
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Annotated, Any, Literal, TypedDict

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Command, interrupt
from pydantic import ValidationError

from agentplat.messages import Message, Role, ToolCall
from agentplat.providers.base import ModelProvider
from agentplat.tools.base import RiskLevel, Tool, ToolContext, ToolError
from agentplat.tools.registry import ToolRegistry

APPROVAL_RISKS = frozenset({RiskLevel.DESTRUCTIVE, RiskLevel.EXTERNAL})


class AgentState(TypedDict):
    # Messages are stored as plain dicts so any checkpointer can serialize them.
    messages: Annotated[list[dict[str, Any]], operator.add]
    steps: int


@dataclass(frozen=True)
class Principal:
    tenant_id: str
    user_id: str
    permissions: frozenset[str]


@dataclass(frozen=True)
class ApprovalRequest:
    tool: str
    args: dict[str, Any]
    reason: str


def idempotency_key(thread_id: str, call: ToolCall) -> str:
    blob = json.dumps([thread_id, call.id, call.name, call.arguments], sort_keys=True)
    return hashlib.sha256(blob.encode()).hexdigest()[:32]


def _needs_approval(tool: Tool[Any]) -> str | None:
    if tool.requires_approval:
        return f"{tool.name} requires approval"
    if tool.risk_level in APPROVAL_RISKS:
        return f"{tool.risk_level.value} tools always require approval"
    return None


def _observation(call: ToolCall, content: str) -> dict[str, Any]:
    return Message(Role.TOOL, content, tool_call_id=call.id, name=call.name).to_dict()


def build_graph(
    provider: ModelProvider,
    registry: ToolRegistry,
    *,
    principal: Principal,
    allowed_tools: Iterable[str],
    checkpointer: BaseCheckpointSaver[Any],
    max_steps: int = 8,
    on_tool_start: Callable[[str], None] | None = None,
) -> CompiledStateGraph[AgentState, None, AgentState, AgentState]:
    allowed = frozenset(allowed_tools)
    schemas = registry.schemas(allowed)

    async def agent_node(state: AgentState) -> dict[str, Any]:
        history = [Message.from_dict(m) for m in state["messages"]]
        response = await provider.complete(history, schemas)
        msg = Message(Role.ASSISTANT, response.content or "", tool_calls=list(response.tool_calls))
        return {"messages": [msg.to_dict()], "steps": state["steps"] + 1}

    def route(state: AgentState) -> Literal["tools", "__end__"]:
        last = Message.from_dict(state["messages"][-1])
        if last.tool_calls and state["steps"] < max_steps:
            return "tools"
        return "__end__"  # == langgraph.graph.END

    async def tools_node(state: AgentState, config: RunnableConfig) -> dict[str, Any]:
        # NOTE: on resume after interrupt() LangGraph re-runs this whole node from
        # the top. Anything executed before the interrupt runs again, which is why
        # every call gets an idempotency key (see test_side_effects_before_interrupt).
        thread_id: str = config.get("configurable", {})["thread_id"]
        last = Message.from_dict(state["messages"][-1])
        observations = []
        for call in last.tool_calls:
            observations.append(await _run_call(thread_id, call))
        return {"messages": observations}

    async def _run_call(thread_id: str, call: ToolCall) -> dict[str, Any]:
        # 1. authorization: agent allow-list AND the end user's permissions
        if call.name not in allowed or call.name not in registry:
            return _observation(call, f"denied: tool '{call.name}' is not allowed for this agent")
        tool = registry.get(call.name)
        missing = tool.required_permissions - principal.permissions
        if missing:
            return _observation(call, f"denied: missing permissions {sorted(missing)}")
        # 2. schema validation (extra fields rejected by ToolArgs)
        try:
            args = tool.parse_args(call.arguments)
        except ValidationError as e:
            return _observation(call, f"invalid arguments: {e.errors()[0]['msg']}")
        # 3. approval gate: pause the graph and wait for a human
        reason = _needs_approval(tool)
        if reason:
            decision = interrupt(
                {"tool": call.name, "args": call.arguments, "reason": reason, "call_id": call.id}
            )
            if decision.get("decision") != "approve":
                comment = decision.get("comment", "")
                return _observation(call, f"rejected by a human reviewer: {comment}".strip())
        # 4. execute with timeout + idempotency key
        ctx = ToolContext(
            run_id=thread_id,
            user_id=principal.user_id,
            tenant_id=principal.tenant_id,
            idempotency_key=idempotency_key(thread_id, call),
        )
        if on_tool_start:
            on_tool_start(call.name)
        try:
            async with asyncio.timeout(tool.timeout_s):
                result = await tool.run(args, ctx)
        except TimeoutError:
            return _observation(call, f"error: {call.name} timed out")
        except ToolError as e:
            return _observation(call, f"error: {e}")
        return _observation(call, json.dumps(result.output, default=str))

    graph = StateGraph(AgentState)
    graph.add_node("agent", agent_node)
    graph.add_node("tools", tools_node)
    graph.add_edge(START, "agent")
    graph.add_conditional_edges("agent", route, ["tools", END])
    graph.add_edge("tools", "agent")
    return graph.compile(checkpointer=checkpointer)


# --- thin driver, the equivalent of Runtime.execute + approvals.decide ---------


@dataclass
class Outcome:
    status: Literal["succeeded", "awaiting_approval"]
    final_output: str | None
    pending: list[ApprovalRequest]
    messages: list[Message]


def _config(thread_id: str) -> RunnableConfig:
    return {"configurable": {"thread_id": thread_id}}


async def _outcome(
    graph: CompiledStateGraph[AgentState, None, AgentState, AgentState], thread_id: str
) -> Outcome:
    snapshot = await graph.aget_state(_config(thread_id))
    messages = [Message.from_dict(m) for m in snapshot.values.get("messages", [])]
    pending = [
        ApprovalRequest(i.value["tool"], i.value["args"], i.value["reason"])
        for i in snapshot.interrupts
    ]
    if pending:
        return Outcome("awaiting_approval", None, pending, messages)
    final = messages[-1].content if messages else None
    return Outcome("succeeded", final, [], messages)


async def start(
    graph: CompiledStateGraph[AgentState, None, AgentState, AgentState],
    thread_id: str,
    *,
    system_prompt: str,
    user_input: str,
) -> Outcome:
    initial: AgentState = {
        "messages": [
            Message(Role.SYSTEM, system_prompt).to_dict(),
            Message(Role.USER, user_input).to_dict(),
        ],
        "steps": 0,
    }
    await graph.ainvoke(initial, _config(thread_id))
    return await _outcome(graph, thread_id)


async def resume(
    graph: CompiledStateGraph[AgentState, None, AgentState, AgentState],
    thread_id: str,
    *,
    decision: Literal["approve", "reject"],
    comment: str = "",
) -> Outcome:
    await graph.ainvoke(
        Command(resume={"decision": decision, "comment": comment}), _config(thread_id)
    )
    return await _outcome(graph, thread_id)
