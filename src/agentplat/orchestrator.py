"""The agent runtime: a durable, resumable agent loop.

    model proposes -> guard decides -> executor runs -> observation persisted -> repeat

Every step is persisted *before* the next one starts, so if a worker dies the
run can be resumed by another worker from exactly where it stopped. The model's
output is treated as a proposal from an untrusted planner, never as a command.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import replace
from typing import Any, Protocol

from pydantic import ValidationError

from agentplat.guards import AllowListGuard, Decision, Verdict
from agentplat.messages import Message, ModelResponse, Role, ToolCall
from agentplat.providers.base import ModelProvider
from agentplat.runtime.events import EventPublisher
from agentplat.runtime.executor import ToolExecutor
from agentplat.state import RunStatus
from agentplat.store.models import Agent, Run, ToolCallRecord
from agentplat.store.sql import NotFoundError, SqlStore, new_id
from agentplat.tools.base import Tool, ToolContext
from agentplat.tools.registry import ToolRegistry, UnknownToolError


class Guard(Protocol):
    async def decide(self, run: Run, agent: Agent, call: ToolCall, tool: Tool[Any]) -> Decision: ...


ProviderFor = Callable[[Agent], ModelProvider]


def idempotency_key(run_id: str, call: ToolCall) -> str:
    canonical = json.dumps(call.arguments, sort_keys=True, separators=(",", ":"), default=str)
    raw = f"{run_id}|{call.id}|{call.name}|{canonical}"
    return hashlib.sha256(raw.encode()).hexdigest()[:48]


def unanswered_calls(messages: list[Message]) -> list[ToolCall]:
    """Tool calls from the latest assistant turn that have no observation yet."""
    for i in range(len(messages) - 1, -1, -1):
        m = messages[i]
        if m.role is Role.ASSISTANT:
            answered = {x.tool_call_id for x in messages[i + 1 :] if x.role is Role.TOOL}
            return [c for c in m.tool_calls if c.id not in answered]
    return []


def observation(payload: Any) -> str:
    return payload if isinstance(payload, str) else json.dumps(payload, default=str)


class Paused(Exception):  # noqa: N818 - a control-flow signal, not an error
    """Internal signal: the run must wait for a human decision."""


class Runtime:
    def __init__(
        self,
        store: SqlStore,
        registry: ToolRegistry,
        provider_for: ProviderFor,
        events: EventPublisher,
        *,
        worker_id: str = "worker-local",
        lease_ttl_s: float = 30.0,
        guard: Guard | None = None,
        executor: ToolExecutor | None = None,
    ) -> None:
        self.store = store
        self.registry = registry
        self.provider_for = provider_for
        self.events = events
        self.worker_id = worker_id
        self.lease_ttl_s = lease_ttl_s
        self.guard: Guard = guard or AllowListGuard()
        self.executor = executor or ToolExecutor()

    # -- public API ----------------------------------------------------------------

    async def start_run(self, agent: Agent, user_id: str, user_input: str) -> Run:
        run = Run(
            id=new_id(),
            tenant_id=agent.tenant_id,
            agent_id=agent.id,
            agent_version=agent.version,
            user_id=user_id,
            status=RunStatus.QUEUED,
            input=user_input,
            messages=[Message(Role.SYSTEM, agent.system_prompt), Message(Role.USER, user_input)],
        )
        await self.store.create_run(run)
        await self.events.emit(run.id, "status", status=run.status.value)
        return run

    async def execute(self, run_id: str) -> Run:
        """Drive a run until it finishes, pauses for approval, or the worker dies.

        Safe to call concurrently from many workers: only the lease holder proceeds.
        """
        if not await self.store.acquire_lease(run_id, self.worker_id, self.lease_ttl_s):
            return await self.store.get_run(run_id)
        try:
            run = await self._drive(run_id)
        except Exception as exc:  # BaseException (a real crash, cancellation) escapes
            run = await self._fail(run_id, exc)
        await self.store.release_lease(run_id, self.worker_id)
        return run

    # -- the loop ------------------------------------------------------------------

    async def _drive(self, run_id: str) -> Run:
        run = await self.store.get_run(run_id)
        if run.status.is_terminal or run.status is RunStatus.AWAITING_APPROVAL:
            return run
        if run.status is RunStatus.QUEUED:
            await self._set_status(run, RunStatus.RUNNING)
        agent = await self.store.get_agent(run.agent_id)
        provider = self.provider_for(agent)

        while True:
            run = await self._refresh_cancel(run)
            if run.cancel_requested:
                return await self._finish(run, RunStatus.CANCELLED)

            pending = unanswered_calls(run.messages)
            if pending:
                try:
                    await self._process_calls(run, agent, pending)
                except Paused:
                    await self._set_status(run, RunStatus.AWAITING_APPROVAL)
                    return run
                continue

            stop = self._check_budgets(run, agent)
            if stop is not None:
                return await self._finish(run, stop)

            await self.events.emit(run.id, "step_started", step=run.step_count + 1)
            response = await self._call_model(run, agent, provider)
            run.step_count += 1
            run.prompt_tokens += response.usage.prompt_tokens
            run.completion_tokens += response.usage.completion_tokens

            if response.is_final:
                text = response.content or ""
                run.messages.append(Message(Role.ASSISTANT, text))
                run.final_output = text
                await self.store.save_progress(run)
                await self.events.emit(run.id, "final", output=text)
                return await self._finish(run, RunStatus.SUCCEEDED)

            calls = [
                replace(c, id=f"call_{run.step_count}_{i}")
                for i, c in enumerate(response.tool_calls)
            ]
            run.messages.append(Message(Role.ASSISTANT, response.content or "", tool_calls=calls))
            # Persist the proposal BEFORE executing anything: on a crash, the resumed
            # run re-derives the same call ids and idempotency keys from this state.
            await self.store.save_progress(run)
            for c in calls:
                await self.events.emit(run.id, "tool_call_proposed", tool=c.name, args=c.arguments)
            await self.store.renew_lease(run.id, self.worker_id, self.lease_ttl_s)

    async def _call_model(self, run: Run, agent: Agent, provider: ModelProvider) -> ModelResponse:
        schemas = [self.registry.get(n).schema() for n in agent.allowed_tools if n in self.registry]
        return await provider.complete(run.messages, schemas, model=agent.model_config.get("model"))

    def _check_budgets(self, run: Run, agent: Agent) -> RunStatus | None:
        if run.step_count >= agent.max_steps:
            return RunStatus.MAX_STEPS
        if run.prompt_tokens + run.completion_tokens >= agent.max_tokens:
            return RunStatus.BUDGET_EXCEEDED
        return None

    # -- tool calls ------------------------------------------------------------------

    async def _process_calls(self, run: Run, agent: Agent, pending: list[ToolCall]) -> None:
        for call in pending:
            obs = await self._handle_call(run, agent, call)
            run.messages.append(Message(Role.TOOL, obs, tool_call_id=call.id, name=call.name))
            await self.store.save_progress(run)

    async def _handle_call(self, run: Run, agent: Agent, call: ToolCall) -> str:
        key = idempotency_key(run.id, call)
        rec = await self.store.get_tool_call(run.id, call.id)
        if rec is not None and rec.status in {"succeeded", "failed", "denied"}:
            # Executed before a crash, but the observation was never persisted.
            return self._observation_from(rec)

        try:
            tool = self.registry.get(call.name)
        except UnknownToolError:
            return await self._deny(run, call, key, f"unknown tool {call.name!r}")
        try:
            decision = await self.guard.decide(run, agent, call, tool)
        except ValidationError as exc:
            return await self._deny(
                run,
                call,
                key,
                "invalid arguments",
                details=exc.errors(include_url=False, include_context=False),
            )
        if decision.verdict is Verdict.DENY:
            return await self._deny(run, call, key, decision.reason)
        if decision.verdict is Verdict.APPROVAL:
            raise Paused  # handled in M4
        return await self._run_tool(run, call, tool, decision, key, rec)

    async def _run_tool(
        self,
        run: Run,
        call: ToolCall,
        tool: Tool[Any],
        decision: Decision,
        key: str,
        rec: ToolCallRecord | None,
    ) -> str:
        rec = rec or ToolCallRecord(
            id=call.id,
            run_id=run.id,
            step=run.step_count,
            tool_name=call.name,
            args=call.arguments,
            idempotency_key=key,
            decision=decision.verdict.value,
            decision_reason=decision.reason,
            status="started",
        )
        rec.status = "started"
        await self.store.upsert_tool_call(rec)  # "started" marker survives a crash
        ctx = ToolContext(run.id, run.user_id, run.tenant_id, idempotency_key=key)
        outcome = await self.executor.execute(tool, decision.args, ctx)
        rec.attempts += outcome.attempts
        rec.latency_ms = outcome.latency_ms
        if outcome.result is not None:
            rec.status, rec.output = "succeeded", outcome.result.output
            rec.output_tainted = outcome.result.tainted
        else:
            rec.status, rec.error = "failed", outcome.error
        await self.store.upsert_tool_call(rec)
        await self.events.emit(
            run.id, "tool_result", tool=call.name, ok=outcome.ok, error=outcome.error
        )
        return self._observation_from(rec)

    async def _deny(
        self, run: Run, call: ToolCall, key: str, reason: str, *, details: Any = None
    ) -> str:
        await self.store.upsert_tool_call(
            ToolCallRecord(
                id=call.id,
                run_id=run.id,
                step=run.step_count,
                tool_name=call.name,
                args=call.arguments,
                idempotency_key=key,
                decision=Verdict.DENY.value,
                decision_reason=reason,
                status="denied",
                error=reason,
                output={"details": details} if details else None,
            )
        )
        await self.events.emit(run.id, "tool_call_denied", tool=call.name, reason=reason)
        payload: dict[str, Any] = {"error": reason}
        if details:
            payload["details"] = details
        return observation(payload)

    @staticmethod
    def _observation_from(rec: ToolCallRecord) -> str:
        if rec.status == "succeeded":
            return observation(rec.output)
        payload: dict[str, Any] = {"error": rec.error or rec.decision_reason or "failed"}
        if isinstance(rec.output, dict) and rec.output.get("details"):
            payload["details"] = rec.output["details"]
        return observation(payload)

    # -- status helpers ----------------------------------------------------------------

    async def _set_status(self, run: Run, target: RunStatus) -> None:
        if not await self.store.transition(run.id, run.status, target):
            current = (await self.store.get_run(run.id)).status
            raise RuntimeError(f"run {run.id} moved to {current} concurrently")
        run.status = target
        await self.events.emit(run.id, "status", status=target.value)

    async def _finish(self, run: Run, status: RunStatus) -> Run:
        await self.store.save_progress(run)
        await self._set_status(run, status)
        return run

    async def _fail(self, run_id: str, exc: Exception) -> Run:
        try:
            run = await self.store.get_run(run_id)
        except NotFoundError:
            raise exc from None
        if run.status.is_terminal:
            return run
        run.error = f"{type(exc).__name__}: {exc}"[:2000]
        await self.store.save_progress(run)
        if run.status is not RunStatus.RUNNING:  # e.g. crashed while queued
            await self.store.transition(run.id, run.status, RunStatus.RUNNING)
            run.status = RunStatus.RUNNING
        await self._set_status(run, RunStatus.FAILED)
        await self.events.emit(run.id, "error", error=run.error)
        return run

    async def _refresh_cancel(self, run: Run) -> Run:
        fresh = await self.store.get_run(run.id)
        run.cancel_requested = fresh.cancel_requested
        return run
