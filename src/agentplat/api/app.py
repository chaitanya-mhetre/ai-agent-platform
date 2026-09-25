from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import sqlalchemy as sa
from fastapi import FastAPI, HTTPException, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from sse_starlette.sse import EventSourceResponse

from agentplat import __version__
from agentplat.api.deps import ContainerDep, PrincipalDep
from agentplat.api.schemas import AgentIn, ApprovalDecisionIn, PermissionsIn, RunIn
from agentplat.approvals import ApprovalError, decide
from agentplat.container import Container, build_container
from agentplat.observability import metrics
from agentplat.observability.tracing import build_trace_tree
from agentplat.state import RunStatus
from agentplat.store.models import Agent, Run
from agentplat.store.sql import NotFoundError, new_id

TERMINAL = {s.value for s in RunStatus if s.is_terminal}


def create_app(container: Container | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        c = container or build_container()
        app.state.container = c
        if c.settings.auto_migrate:
            await c.store.migrate()
        await c.bootstrap()
        stop = asyncio.Event()
        worker_task = None
        if c.settings.embedded_worker:
            worker_task = asyncio.create_task(c.worker().run_forever(stop))
        yield
        stop.set()
        if worker_task:
            with contextlib.suppress(asyncio.CancelledError):
                await worker_task
        if container is None:
            await c.close()

    app = FastAPI(title="agentplat", version=__version__, lifespan=lifespan)

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/ready")
    async def ready(c: ContainerDep) -> dict[str, str]:
        async with c.store.engine.connect() as conn:
            await conn.execute(sa.text("SELECT 1"))
        return {"status": "ready"}

    @app.get("/metrics")
    async def prometheus_metrics(c: ContainerDep) -> Response:
        metrics.APPROVALS_PENDING.set(await c.store.count_pending_approvals())
        return Response(generate_latest(metrics.REGISTRY), media_type=CONTENT_TYPE_LATEST)

    # -- agents & tools ----------------------------------------------------------

    @app.post("/v1/agents", status_code=201)
    async def create_agent(body: AgentIn, c: ContainerDep, p: PrincipalDep) -> dict[str, Any]:
        unknown = [t for t in body.allowed_tools if t not in c.registry]
        if unknown:
            raise HTTPException(422, f"unknown tools: {unknown}")
        agent = Agent(
            id=new_id(),
            tenant_id=p.tenant_id,
            name=body.name,
            system_prompt=body.system_prompt,
            allowed_tools=body.allowed_tools,
            model_config=body.model_config_,
            max_steps=body.max_steps,
            max_cost_usd=body.max_cost_usd,
            max_tokens=body.max_tokens,
        )
        await c.store.create_agent(agent)
        await c.store.audit(p.tenant_id, "user", p.user_id, "agent.create", agent.id)
        return {"id": agent.id, "name": agent.name, "allowed_tools": agent.allowed_tools}

    @app.get("/v1/agents")
    async def list_agents(c: ContainerDep, p: PrincipalDep) -> list[dict[str, Any]]:
        return [
            {"id": a.id, "name": a.name, "allowed_tools": a.allowed_tools}
            for a in await c.store.list_agents(p.tenant_id)
        ]

    @app.get("/v1/tools")
    async def list_tools(c: ContainerDep) -> list[dict[str, Any]]:
        return [t.describe() for t in c.registry]

    # -- runs ----------------------------------------------------------------------

    async def load_run(c: Container, run_id: str, tenant_id: str) -> Run:
        try:
            return await c.store.get_run(run_id, tenant_id)
        except NotFoundError:
            raise HTTPException(404, "run not found") from None

    @app.post("/v1/runs", status_code=201)
    async def create_run(body: RunIn, c: ContainerDep, p: PrincipalDep) -> dict[str, Any]:
        limit = c.settings.rate_limit_per_minute
        for key, n in (
            (f"user:{p.tenant_id}:{p.user_id}", limit),
            (f"tenant:{p.tenant_id}", limit * 10),
        ):
            allowed, retry_after = await c.limiter.hit(key, n)
            if not allowed:
                raise HTTPException(
                    429, "rate limit exceeded", headers={"Retry-After": str(retry_after)}
                )
        try:
            agent = await c.store.get_agent(body.agent_id, p.tenant_id)
        except NotFoundError:
            raise HTTPException(404, "agent not found") from None
        run = await c.runtime.start_run(agent, p.user_id, body.input)
        await c.store.audit(p.tenant_id, "user", p.user_id, "run.create", run.id)
        if body.mode == "sync":
            run = await c.runtime.execute(run.id)
        else:
            await c.queue.enqueue(run.id)
        return run.summary()

    @app.get("/v1/runs")
    async def list_runs(c: ContainerDep, p: PrincipalDep) -> list[dict[str, Any]]:
        return await c.store.list_runs(p.tenant_id)

    @app.get("/v1/runs/{run_id}")
    async def get_run(run_id: str, c: ContainerDep, p: PrincipalDep) -> dict[str, Any]:
        run = await load_run(c, run_id, p.tenant_id)
        out = run.summary()
        out["messages"] = [m.to_dict() for m in run.messages]
        out["tool_calls"] = [
            {
                "id": r.id,
                "tool": r.tool_name,
                "args": r.args,
                "decision": r.decision,
                "decision_reason": r.decision_reason,
                "status": r.status,
                "attempts": r.attempts,
                "latency_ms": r.latency_ms,
                "error": r.error,
            }
            for r in await c.store.list_tool_calls(run_id)
        ]
        return out

    @app.post("/v1/runs/{run_id}/cancel", status_code=202)
    async def cancel_run(run_id: str, c: ContainerDep, p: PrincipalDep) -> dict[str, str]:
        run = await load_run(c, run_id, p.tenant_id)
        if run.status.is_terminal:
            raise HTTPException(409, f"run already {run.status.value}")
        if run.status is RunStatus.AWAITING_APPROVAL and await c.store.transition(
            run_id, RunStatus.AWAITING_APPROVAL, RunStatus.CANCELLED
        ):
            await c.runtime.events.emit(run_id, "status", status="cancelled")
            return {"status": "cancelled"}
        await c.store.request_cancel(run_id)
        await c.store.audit(p.tenant_id, "user", p.user_id, "run.cancel", run_id)
        return {"status": "cancel_requested"}

    @app.get("/v1/runs/{run_id}/trace")
    async def run_trace(run_id: str, c: ContainerDep, p: PrincipalDep) -> dict[str, Any]:
        run = await load_run(c, run_id, p.tenant_id)
        spans = await c.store.list_spans(run_id)
        model_spans = [s for s in spans if s["kind"] == "model_call"]
        tool_spans = [s for s in spans if s["kind"] == "tool_call"]
        return {
            "run_id": run_id,
            "status": run.status.value,
            "totals": {
                "model_calls": len(model_spans),
                "tool_calls": len(tool_spans),
                "prompt_tokens": run.prompt_tokens,
                "completion_tokens": run.completion_tokens,
                "est_cost_usd": round(run.est_cost_usd, 6),
                "model_time_ms": round(sum(s["duration_ms"] or 0 for s in model_spans), 3),
                "tool_time_ms": round(sum(s["duration_ms"] or 0 for s in tool_spans), 3),
            },
            "spans": build_trace_tree(spans),
        }

    @app.get("/v1/runs/{run_id}/events")
    async def run_events(run_id: str, c: ContainerDep, p: PrincipalDep) -> EventSourceResponse:
        await load_run(c, run_id, p.tenant_id)
        return EventSourceResponse(stream_events(c, run_id))

    # -- approvals -----------------------------------------------------------------

    @app.get("/v1/approvals")
    async def list_approvals(
        c: ContainerDep, p: PrincipalDep, status: str | None = "pending"
    ) -> list[dict[str, Any]]:
        return [a.to_dict() for a in await c.store.list_approvals(p.tenant_id, status)]

    @app.post("/v1/approvals/{approval_id}")
    async def decide_approval(
        approval_id: str, body: ApprovalDecisionIn, c: ContainerDep, p: PrincipalDep
    ) -> dict[str, Any]:
        try:
            approval = await decide(
                c,
                tenant_id=p.tenant_id,
                user_id=p.user_id,
                approval_id=approval_id,
                decision=body.decision,
                edited_args=body.edited_args,
                comment=body.comment,
            )
        except NotFoundError:
            raise HTTPException(404, "approval not found") from None
        except ApprovalError as exc:
            raise HTTPException(exc.status, str(exc)) from None
        return approval.to_dict()

    # -- permissions & audit (admin) ---------------------------------------------------

    async def require_admin(c: Container, tenant_id: str, user_id: str) -> None:
        if "admin" not in await c.store.permissions(tenant_id, user_id):
            raise HTTPException(403, "admin only")

    @app.get("/v1/users/{user_id}/permissions")
    async def get_permissions(user_id: str, c: ContainerDep, p: PrincipalDep) -> list[str]:
        if user_id != p.user_id:
            await require_admin(c, p.tenant_id, p.user_id)
        return sorted(await c.store.permissions(p.tenant_id, user_id))

    @app.post("/v1/users/{user_id}/permissions", status_code=201)
    async def grant_permissions(
        user_id: str, body: PermissionsIn, c: ContainerDep, p: PrincipalDep
    ) -> list[str]:
        await require_admin(c, p.tenant_id, p.user_id)
        await c.store.grant(p.tenant_id, user_id, body.permissions)
        await c.store.audit(
            p.tenant_id,
            "user",
            p.user_id,
            "permissions.grant",
            user_id,
            {"granted": body.permissions},
        )
        return sorted(await c.store.permissions(p.tenant_id, user_id))

    @app.get("/v1/audit")
    async def audit(c: ContainerDep, p: PrincipalDep) -> list[dict[str, Any]]:
        await require_admin(c, p.tenant_id, p.user_id)
        return await c.store.list_audit(p.tenant_id)

    return app


async def stream_events(
    c: Container, run_id: str, *, idle_timeout_s: float = 300.0
) -> AsyncIterator[dict[str, str]]:
    """Replay persisted events, then tail live ones.

    Subscribe *before* replaying so nothing published in between is lost; the
    sequence number de-duplicates events seen in both.
    """
    async with c.bus.subscribe(run_id) as live:
        last_seq = 0
        for ev in await c.store.list_events(run_id):
            last_seq = ev["seq"]
            yield _sse(ev)
            if _is_terminal(ev):
                return
        run = await c.store.get_run(run_id)
        if run.status.is_terminal:
            return
        while True:
            try:
                async with asyncio.timeout(idle_timeout_s):
                    ev = await anext(live)
            except TimeoutError:
                return
            if ev["seq"] <= last_seq:
                continue
            last_seq = ev["seq"]
            yield _sse(ev)
            if _is_terminal(ev):
                return


def _is_terminal(ev: dict[str, Any]) -> bool:
    return ev["type"] == "status" and ev["data"].get("status") in TERMINAL


def _sse(ev: dict[str, Any]) -> dict[str, str]:
    return {"id": str(ev["seq"]), "event": ev["type"], "data": json.dumps(ev["data"], default=str)}


app = create_app()
