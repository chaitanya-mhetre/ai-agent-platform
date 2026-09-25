"""The agent runtime: a durable, resumable agent loop.

    model proposes -> guard decides -> executor runs -> observation persisted -> repeat

Every step is persisted *before* the next one starts, so if a worker dies the
run can be resumed by another worker from exactly where it stopped. The model's
output is treated as a proposal from an untrusted planner, never as a command.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable
from dataclasses import replace
from typing import Any, Protocol

from pydantic import ValidationError

from agentplat.guards import AllowListGuard, Decision, Verdict
from agentplat.messages import Message, ModelResponse, Role, ToolCall
from agentplat.observability import metrics as m
from agentplat.observability.pricing import PriceTable
from agentplat.observability.tracing import Span, Tracer
from agentplat.providers.base import ModelProvider
from agentplat.runtime.events import EventPublisher
from agentplat.runtime.executor import ToolExecutor
from agentplat.security.redaction import Redactor, SecretStore
from agentplat.security.taint import SECURITY_PREAMBLE, fence_untrusted, injection_signals
from agentplat.state import RunStatus
from agentplat.store.models import Agent, Approval, Run, ToolCallRecord
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
        secrets: SecretStore | None = None,
        redactor: Redactor | None = None,
        max_observation_chars: int = 8000,
        tracer: Tracer | None = None,
        prices: PriceTable | None = None,
    ) -> None:
        self.store = store
        self.registry = registry
        self.provider_for = provider_for
        self.events = events
        self.worker_id = worker_id
        self.lease_ttl_s = lease_ttl_s
        self.guard: Guard = guard or AllowListGuard()
        self.executor = executor or ToolExecutor()
        self.secrets = secrets or SecretStore()
        self.redactor = redactor or Redactor(self.secrets.all_values())
        self.max_observation_chars = max_observation_chars
        self.tracer = tracer or Tracer(store, self.redactor)
        self.prices = prices or PriceTable({})

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
            messages=[
                Message(Role.SYSTEM, agent.system_prompt + SECURITY_PREAMBLE),
                Message(Role.USER, user_input),
            ],
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
            async with self.tracer.span(
                run_id, "run", "run.execute", worker=self.worker_id
            ) as root:
                run = await self._drive(run_id, root)
                root.set(status=run.status.value, steps=run.step_count)
        except Exception as exc:  # BaseException (a real crash, cancellation) escapes
            run = await self._fail(run_id, exc)
        await self.store.release_lease(run_id, self.worker_id)
        return run

    # -- the loop ------------------------------------------------------------------

    async def _drive(self, run_id: str, root: Span) -> Run:
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
                    await self._process_calls(run, agent, pending, root)
                except Paused:
                    await self._set_status(run, RunStatus.AWAITING_APPROVAL)
                    return run
                continue

            stop = self._check_budgets(run, agent)
            if stop is not None:
                return await self._finish(run, stop)

            await self.events.emit(run.id, "step_started", step=run.step_count + 1)
            response = await self._call_model(run, agent, provider, root)
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

    async def _call_model(
        self, run: Run, agent: Agent, provider: ModelProvider, root: Span
    ) -> ModelResponse:
        schemas = [self.registry.get(n).schema() for n in agent.allowed_tools if n in self.registry]
        model_name = agent.model_config.get("model")
        async with self.tracer.span(
            run.id, "model_call", f"model.{provider.name}", root, step=run.step_count + 1
        ) as span:
            t0 = time.perf_counter()
            response = await provider.complete(run.messages, schemas, model=model_name)
            latency = time.perf_counter() - t0
            model = response.model or str(model_name or provider.name)
            cost = self.prices.cost(model, response.usage)
            if cost is None:
                m.UNPRICED_CALLS.labels(model).inc()
            else:
                run.est_cost_usd += cost
                m.COST.labels(model).inc(cost)
            m.MODEL_LATENCY.labels(model).observe(latency)
            m.TOKENS.labels(model, "prompt").inc(response.usage.prompt_tokens)
            m.TOKENS.labels(model, "completion").inc(response.usage.completion_tokens)
            span.set(
                model=model,
                prompt_tokens=response.usage.prompt_tokens,
                completion_tokens=response.usage.completion_tokens,
                cost_usd=cost if cost is not None else "unknown",
                finish_reason=response.finish_reason,
                tool_calls=[c.name for c in response.tool_calls],
            )
        return response

    def _check_budgets(self, run: Run, agent: Agent) -> RunStatus | None:
        if run.step_count >= agent.max_steps:
            return RunStatus.MAX_STEPS
        if run.prompt_tokens + run.completion_tokens >= agent.max_tokens:
            return RunStatus.BUDGET_EXCEEDED
        if run.est_cost_usd >= agent.max_cost_usd:
            return RunStatus.BUDGET_EXCEEDED
        return None

    # -- tool calls ------------------------------------------------------------------

    async def _process_calls(
        self, run: Run, agent: Agent, pending: list[ToolCall], root: Span
    ) -> None:
        for call in pending:
            async with self.tracer.span(run.id, "tool_call", f"tool.{call.name}", root) as span:
                obs = await self._handle_call(run, agent, call, span)
            run.messages.append(Message(Role.TOOL, obs, tool_call_id=call.id, name=call.name))
            await self.store.save_progress(run)

    async def _handle_call(self, run: Run, agent: Agent, call: ToolCall, span: Span) -> str:
        rec = await self.store.get_tool_call(run.id, call.id)
        if rec is not None and rec.status in {"succeeded", "failed", "denied"}:
            # Executed before a crash, but the observation was never persisted.
            return self._observation_from(rec)

        try:
            tool = self.registry.get(call.name)
        except UnknownToolError:
            return await self._deny(run, call, f"unknown tool {call.name!r}")
        try:
            decision = await self.guard.decide(run, agent, call, tool)
        except ValidationError as exc:
            return await self._deny(
                run,
                call,
                "invalid arguments",
                details=exc.errors(include_url=False, include_context=False),
            )
        span.set(
            decision=decision.verdict.value, reason=decision.reason, risk=tool.risk_level.value
        )
        m.TOOL_CALLS.labels(call.name, decision.verdict.value).inc()
        if decision.verdict is Verdict.DENY:
            return await self._deny(run, call, decision.reason)
        if decision.verdict is Verdict.APPROVAL:
            await self._request_approval(run, call, tool, decision)
            raise Paused
        obs = await self._run_tool(run, call, tool, decision, rec)
        rec = await self.store.get_tool_call(run.id, call.id)
        if rec is not None:
            span.set(status=rec.status, attempts=rec.attempts, tainted=rec.output_tainted)
            if rec.status == "failed":
                span.fail(rec.error or "failed")
            if rec.latency_ms is not None:
                m.TOOL_LATENCY.labels(call.name).observe(rec.latency_ms / 1000)
        return obs

    async def _request_approval(
        self, run: Run, call: ToolCall, tool: Tool[Any], decision: Decision
    ) -> None:
        if decision.approval is not None:
            return  # already requested; still pending
        approval = await self.store.create_approval(
            Approval(
                id=new_id(),
                tenant_id=run.tenant_id,
                run_id=run.id,
                tool_call_id=call.id,
                tool_name=call.name,
                args=call.arguments,
                reason=decision.reason,
            )
        )
        await self.store.upsert_tool_call(
            ToolCallRecord(
                id=call.id,
                run_id=run.id,
                step=run.step_count,
                tool_name=call.name,
                args=call.arguments,
                idempotency_key=idempotency_key(run.id, call),
                decision=Verdict.APPROVAL.value,
                decision_reason=decision.reason,
                status="awaiting_approval",
            )
        )
        await self._audit(
            run.tenant_id,
            "agent",
            run.agent_id,
            "tool.approval_requested",
            call.name,
            {"run_id": run.id, "approval_id": approval.id, "reason": decision.reason},
        )
        await self.events.emit(
            run.id,
            "awaiting_approval",
            approval_id=approval.id,
            tool=call.name,
            args=call.arguments,
            reason=decision.reason,
        )

    async def _run_tool(
        self,
        run: Run,
        call: ToolCall,
        tool: Tool[Any],
        decision: Decision,
        rec: ToolCallRecord | None,
    ) -> str:
        effective = replace(call, arguments=decision.arguments or call.arguments)
        key = idempotency_key(run.id, effective)
        if rec is None or rec.idempotency_key != key:
            rec = ToolCallRecord(
                id=call.id,
                run_id=run.id,
                step=run.step_count,
                tool_name=call.name,
                args=effective.arguments,
                idempotency_key=key,
                decision=decision.verdict.value,
                decision_reason=decision.reason,
                status="started",
                attempts=rec.attempts if rec else 0,
            )
        rec.status, rec.decision, rec.decision_reason = "started", "allowed", decision.reason
        await self.store.upsert_tool_call(rec)  # "started" marker survives a crash
        ctx = ToolContext(
            run.id,
            run.user_id,
            run.tenant_id,
            idempotency_key=key,
            secrets=self.secrets.for_tool(call.name),  # only this tool's credentials
        )
        assert decision.args is not None
        outcome = await self.executor.execute(tool, decision.args, ctx)
        rec.attempts += outcome.attempts
        rec.latency_ms = outcome.latency_ms
        if outcome.result is not None:
            error = self._validate_output(tool, outcome.result.output)
            if error:
                rec.status, rec.error = "failed", error
            else:
                rec.status, rec.output = "succeeded", outcome.result.output
                if outcome.result.tainted:
                    rec.output_tainted = True
                    rec.output = {"source": outcome.result.source, "content": rec.output}
                    await self._on_untrusted(run, call, outcome.result.output)
        else:
            rec.status, rec.error = "failed", outcome.error
        await self.store.upsert_tool_call(rec)
        await self._audit(
            run.tenant_id,
            "agent",
            run.agent_id,
            "tool.executed" if outcome.ok else "tool.failed",
            call.name,
            {
                "run_id": run.id,
                "args": effective.arguments,
                "reason": decision.reason,
                "attempts": outcome.attempts,
                "error": outcome.error,
            },
        )
        await self.events.emit(
            run.id, "tool_result", tool=call.name, ok=outcome.ok, error=outcome.error
        )
        obs = self._observation_from(rec)
        if effective.arguments != call.arguments:
            obs = observation(
                {
                    "note": "a human reviewer edited the arguments",
                    "executed_with": effective.arguments,
                    "output": obs,
                }
            )
        return obs

    async def _deny(self, run: Run, call: ToolCall, reason: str, *, details: Any = None) -> str:
        if reason in ("invalid arguments",) or reason.startswith("unknown tool"):
            m.TOOL_CALLS.labels(call.name, "denied").inc()
        await self.store.upsert_tool_call(
            ToolCallRecord(
                id=call.id,
                run_id=run.id,
                step=run.step_count,
                tool_name=call.name,
                args=call.arguments,
                idempotency_key=idempotency_key(run.id, call),
                decision=Verdict.DENY.value,
                decision_reason=reason,
                status="denied",
                error=reason,
                output={"details": details} if details else None,
            )
        )
        await self._audit(
            run.tenant_id,
            "agent",
            run.agent_id,
            "tool.denied",
            call.name,
            {"run_id": run.id, "args": call.arguments, "reason": reason},
        )
        await self.events.emit(run.id, "tool_call_denied", tool=call.name, reason=reason)
        payload: dict[str, Any] = {"error": reason}
        if details:
            payload["details"] = details
        return observation(payload)

    def _observation_from(self, rec: ToolCallRecord) -> str:
        if rec.status == "succeeded":
            if rec.output_tainted and isinstance(rec.output, dict):
                text = observation(rec.output.get("content"))
                text = fence_untrusted(self._cap(text), rec.output.get("source") or rec.tool_name)
            else:
                text = self._cap(observation(rec.output))
            return self.redactor.text(text)
        payload: dict[str, Any] = {"error": rec.error or rec.decision_reason or "failed"}
        if isinstance(rec.output, dict) and rec.output.get("details"):
            payload["details"] = rec.output["details"]
        return self.redactor.text(observation(payload))

    def _cap(self, text: str) -> str:
        if len(text) <= self.max_observation_chars:
            return text
        dropped = len(text) - self.max_observation_chars
        return text[: self.max_observation_chars] + f"\n...[truncated {dropped} chars]"

    @staticmethod
    def _validate_output(tool: Tool[Any], output: Any) -> str | None:
        if tool.output_model is None:
            return None
        try:
            tool.output_model.model_validate(output)
        except ValidationError as exc:
            return f"tool returned invalid output ({exc.error_count()} errors)"
        return None

    async def _on_untrusted(self, run: Run, call: ToolCall, output: Any) -> None:
        run.tainted = True
        signals = injection_signals(observation(output))
        if signals:
            m.INJECTION_SIGNALS.labels(call.name).inc()
            await self.events.emit(
                run.id, "injection_suspected", tool=call.name, signals=signals[:5]
            )
            await self._audit(
                run.tenant_id,
                "system",
                "taint-detector",
                "injection.suspected",
                call.name,
                {"run_id": run.id, "signals": signals[:5]},
            )

    async def _audit(
        self,
        tenant_id: str,
        actor: str,
        actor_id: str,
        action: str,
        target: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        await self.store.audit(
            tenant_id, actor, actor_id, action, target, self.redactor.deep(details or {})
        )

    # -- status helpers ----------------------------------------------------------------

    async def _set_status(self, run: Run, target: RunStatus) -> None:
        if not await self.store.transition(run.id, run.status, target):
            current = (await self.store.get_run(run.id)).status
            raise RuntimeError(f"run {run.id} moved to {current} concurrently")
        run.status = target
        if target.is_terminal:
            m.RUNS.labels(target.value).inc()
            m.STEPS_PER_RUN.observe(run.step_count)
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
