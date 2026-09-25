"""Durable state for agents, runs, tool calls, approvals, events, spans and audit.

One implementation serves PostgreSQL (asyncpg) in production and SQLite
(aiosqlite) in tests. Concurrency-sensitive updates (status transitions,
leases, approval decisions) are single compare-and-set UPDATE statements, so
two workers or two API calls can never both "win".
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from agentplat.messages import Message
from agentplat.state import RunStatus, check_transition
from agentplat.store import schema as t
from agentplat.store.models import Agent, Approval, Run, ToolCallRecord


def utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def new_id() -> str:
    return uuid.uuid4().hex


class NotFoundError(KeyError):
    pass


class SqlStore:
    def __init__(self, engine: AsyncEngine) -> None:
        self.engine = engine

    @classmethod
    def from_url(cls, url: str) -> SqlStore:
        return cls(create_async_engine(url, pool_pre_ping=True))

    async def create_schema(self) -> None:
        async with self.engine.begin() as conn:
            await conn.run_sync(t.metadata.create_all)

    async def drop_schema(self) -> None:
        async with self.engine.begin() as conn:
            await conn.run_sync(t.metadata.drop_all)

    async def close(self) -> None:
        await self.engine.dispose()

    # -- agents & permissions ----------------------------------------------------

    async def create_agent(self, agent: Agent) -> Agent:
        async with self.engine.begin() as c:
            await c.execute(
                t.agents.insert().values(
                    id=agent.id,
                    tenant_id=agent.tenant_id,
                    name=agent.name,
                    system_prompt=agent.system_prompt,
                    model_config=agent.model_config,
                    allowed_tools=agent.allowed_tools,
                    max_steps=agent.max_steps,
                    max_cost_usd=agent.max_cost_usd,
                    max_tokens=agent.max_tokens,
                    version=agent.version,
                    created_at=utcnow(),
                )
            )
        return agent

    async def get_agent(self, agent_id: str, tenant_id: str | None = None) -> Agent:
        q = sa.select(t.agents).where(t.agents.c.id == agent_id)
        if tenant_id is not None:
            q = q.where(t.agents.c.tenant_id == tenant_id)
        async with self.engine.connect() as c:
            row = (await c.execute(q)).mappings().first()
        if row is None:
            raise NotFoundError(agent_id)
        return Agent(
            id=row["id"],
            tenant_id=row["tenant_id"],
            name=row["name"],
            system_prompt=row["system_prompt"],
            allowed_tools=list(row["allowed_tools"]),
            model_config=dict(row["model_config"]),
            max_steps=row["max_steps"],
            max_cost_usd=row["max_cost_usd"],
            max_tokens=row["max_tokens"],
            version=row["version"],
        )

    async def list_agents(self, tenant_id: str) -> list[Agent]:
        async with self.engine.connect() as c:
            q = sa.select(t.agents.c.id).where(t.agents.c.tenant_id == tenant_id)
            id_list: list[str] = list((await c.execute(q)).scalars())
        return [await self.get_agent(i) for i in id_list]

    async def grant(self, tenant_id: str, user_id: str, permissions: Sequence[str]) -> None:
        existing = await self.permissions(tenant_id, user_id)
        new = [p for p in permissions if p not in existing]
        if not new:
            return
        async with self.engine.begin() as c:
            await c.execute(
                t.user_permissions.insert(),
                [{"tenant_id": tenant_id, "user_id": user_id, "permission": p} for p in new],
            )

    async def revoke(self, tenant_id: str, user_id: str, permission: str) -> None:
        async with self.engine.begin() as c:
            await c.execute(
                t.user_permissions.delete().where(
                    t.user_permissions.c.tenant_id == tenant_id,
                    t.user_permissions.c.user_id == user_id,
                    t.user_permissions.c.permission == permission,
                )
            )

    async def permissions(self, tenant_id: str, user_id: str) -> frozenset[str]:
        q = sa.select(t.user_permissions.c.permission).where(
            t.user_permissions.c.tenant_id == tenant_id, t.user_permissions.c.user_id == user_id
        )
        async with self.engine.connect() as c:
            return frozenset((await c.execute(q)).scalars())

    # -- runs --------------------------------------------------------------------

    async def create_run(self, run: Run) -> Run:
        now = utcnow()
        run.created_at = run.updated_at = now
        async with self.engine.begin() as c:
            await c.execute(
                t.runs.insert().values(
                    id=run.id,
                    tenant_id=run.tenant_id,
                    agent_id=run.agent_id,
                    agent_version=run.agent_version,
                    user_id=run.user_id,
                    status=run.status.value,
                    input=run.input,
                    messages=[m.to_dict() for m in run.messages],
                    created_at=now,
                    updated_at=now,
                )
            )
        return run

    async def get_run(self, run_id: str, tenant_id: str | None = None) -> Run:
        q = sa.select(t.runs).where(t.runs.c.id == run_id)
        if tenant_id is not None:
            q = q.where(t.runs.c.tenant_id == tenant_id)
        async with self.engine.connect() as c:
            row = (await c.execute(q)).mappings().first()
        if row is None:
            raise NotFoundError(run_id)
        return Run(
            id=row["id"],
            tenant_id=row["tenant_id"],
            agent_id=row["agent_id"],
            agent_version=row["agent_version"],
            user_id=row["user_id"],
            status=RunStatus(row["status"]),
            input=row["input"],
            messages=[Message.from_dict(m) for m in row["messages"]],
            final_output=row["final_output"],
            tainted=row["tainted"],
            step_count=row["step_count"],
            prompt_tokens=row["prompt_tokens"],
            completion_tokens=row["completion_tokens"],
            est_cost_usd=row["est_cost_usd"],
            cancel_requested=row["cancel_requested"],
            error=row["error"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    async def save_progress(self, run: Run) -> None:
        """Persist conversation + counters. Status changes go through `transition`."""
        async with self.engine.begin() as c:
            await c.execute(
                t.runs.update()
                .where(t.runs.c.id == run.id)
                .values(
                    messages=[m.to_dict() for m in run.messages],
                    tainted=run.tainted,
                    step_count=run.step_count,
                    prompt_tokens=run.prompt_tokens,
                    completion_tokens=run.completion_tokens,
                    est_cost_usd=run.est_cost_usd,
                    final_output=run.final_output,
                    error=run.error,
                    updated_at=utcnow(),
                )
            )

    async def transition(self, run_id: str, current: RunStatus, target: RunStatus) -> bool:
        """Compare-and-set the status. Returns False if someone else moved it first."""
        check_transition(current, target)
        async with self.engine.begin() as c:
            res = await c.execute(
                t.runs.update()
                .where(t.runs.c.id == run_id, t.runs.c.status == current.value)
                .values(status=target.value, updated_at=utcnow())
            )
        return res.rowcount == 1

    async def request_cancel(self, run_id: str) -> None:
        async with self.engine.begin() as c:
            await c.execute(
                t.runs.update().where(t.runs.c.id == run_id).values(cancel_requested=True)
            )

    async def list_runs(self, tenant_id: str, limit: int = 50) -> list[dict[str, Any]]:
        q = (
            sa.select(t.runs.c.id, t.runs.c.status, t.runs.c.agent_id, t.runs.c.updated_at)
            .where(t.runs.c.tenant_id == tenant_id)
            .order_by(t.runs.c.updated_at.desc())
            .limit(limit)
        )
        async with self.engine.connect() as c:
            return [dict(r) for r in (await c.execute(q)).mappings()]

    # -- leases ------------------------------------------------------------------

    async def acquire_lease(self, run_id: str, owner: str, ttl_s: float) -> bool:
        now = utcnow()
        async with self.engine.begin() as c:
            res = await c.execute(
                t.runs.update()
                .where(
                    t.runs.c.id == run_id,
                    sa.or_(
                        t.runs.c.lease_owner.is_(None),
                        t.runs.c.lease_owner == owner,
                        t.runs.c.lease_expires_at < now,
                    ),
                )
                .values(lease_owner=owner, lease_expires_at=now + timedelta(seconds=ttl_s))
            )
        return res.rowcount == 1

    async def renew_lease(self, run_id: str, owner: str, ttl_s: float) -> bool:
        async with self.engine.begin() as c:
            res = await c.execute(
                t.runs.update()
                .where(t.runs.c.id == run_id, t.runs.c.lease_owner == owner)
                .values(lease_expires_at=utcnow() + timedelta(seconds=ttl_s))
            )
        return res.rowcount == 1

    async def release_lease(self, run_id: str, owner: str) -> None:
        async with self.engine.begin() as c:
            await c.execute(
                t.runs.update()
                .where(t.runs.c.id == run_id, t.runs.c.lease_owner == owner)
                .values(lease_owner=None, lease_expires_at=None)
            )

    async def reclaimable_runs(self, stale_after_s: float = 30.0) -> list[str]:
        """Runs a worker should pick up: RUNNING with an expired lease (worker died),
        or QUEUED for a while with no lease (enqueue lost)."""
        now = utcnow()
        q = sa.select(t.runs.c.id).where(
            sa.or_(
                sa.and_(
                    t.runs.c.status == "running",
                    sa.or_(t.runs.c.lease_owner.is_(None), t.runs.c.lease_expires_at < now),
                ),
                sa.and_(
                    t.runs.c.status == "queued",
                    t.runs.c.lease_owner.is_(None),
                    t.runs.c.updated_at < now - timedelta(seconds=stale_after_s),
                ),
            )
        )
        async with self.engine.connect() as c:
            return list((await c.execute(q)).scalars())

    # -- tool calls ----------------------------------------------------------------

    async def get_tool_call(self, run_id: str, call_id: str) -> ToolCallRecord | None:
        q = sa.select(t.tool_calls).where(
            t.tool_calls.c.run_id == run_id, t.tool_calls.c.id == call_id
        )
        async with self.engine.connect() as c:
            row = (await c.execute(q)).mappings().first()
        return None if row is None else ToolCallRecord(**dict(row))

    async def upsert_tool_call(self, rec: ToolCallRecord) -> None:
        values = {
            "step": rec.step,
            "tool_name": rec.tool_name,
            "args": rec.args,
            "idempotency_key": rec.idempotency_key,
            "decision": rec.decision,
            "decision_reason": rec.decision_reason,
            "status": rec.status,
            "output": rec.output,
            "output_tainted": rec.output_tainted,
            "latency_ms": rec.latency_ms,
            "attempts": rec.attempts,
            "error": rec.error,
        }
        async with self.engine.begin() as c:
            res = await c.execute(
                t.tool_calls.update()
                .where(t.tool_calls.c.run_id == rec.run_id, t.tool_calls.c.id == rec.id)
                .values(**values)
            )
            if res.rowcount == 0:
                await c.execute(
                    t.tool_calls.insert().values(id=rec.id, run_id=rec.run_id, **values)
                )

    async def list_tool_calls(self, run_id: str) -> list[ToolCallRecord]:
        q = (
            sa.select(t.tool_calls)
            .where(t.tool_calls.c.run_id == run_id)
            .order_by(t.tool_calls.c.step)
        )
        async with self.engine.connect() as c:
            return [ToolCallRecord(**dict(r)) for r in (await c.execute(q)).mappings()]

    # -- approvals -------------------------------------------------------------------

    async def create_approval(self, a: Approval) -> Approval:
        a.requested_at = utcnow()
        async with self.engine.begin() as c:
            await c.execute(
                t.approvals.insert().values(
                    id=a.id,
                    tenant_id=a.tenant_id,
                    run_id=a.run_id,
                    tool_call_id=a.tool_call_id,
                    tool_name=a.tool_name,
                    args=a.args,
                    reason=a.reason,
                    decision=a.decision,
                    requested_at=a.requested_at,
                )
            )
        return a

    async def _approval_where(self, *conds: sa.ColumnElement[bool]) -> Approval | None:
        async with self.engine.connect() as c:
            row = (await c.execute(sa.select(t.approvals).where(*conds))).mappings().first()
        return None if row is None else Approval(**dict(row))

    async def get_approval(self, approval_id: str, tenant_id: str | None = None) -> Approval:
        conds = [t.approvals.c.id == approval_id]
        if tenant_id is not None:
            conds.append(t.approvals.c.tenant_id == tenant_id)
        a = await self._approval_where(*conds)
        if a is None:
            raise NotFoundError(approval_id)
        return a

    async def approval_for_call(self, run_id: str, call_id: str) -> Approval | None:
        return await self._approval_where(
            t.approvals.c.run_id == run_id, t.approvals.c.tool_call_id == call_id
        )

    async def list_approvals(self, tenant_id: str, decision: str | None = None) -> list[Approval]:
        q = sa.select(t.approvals).where(t.approvals.c.tenant_id == tenant_id)
        if decision:
            q = q.where(t.approvals.c.decision == decision)
        async with self.engine.connect() as c:
            rows = (await c.execute(q.order_by(t.approvals.c.requested_at))).mappings()
            return [Approval(**dict(r)) for r in rows]

    async def decide_approval(
        self,
        approval_id: str,
        decision: str,
        *,
        by: str,
        edited_args: dict[str, Any] | None = None,
        comment: str | None = None,
    ) -> bool:
        async with self.engine.begin() as c:
            res = await c.execute(
                t.approvals.update()
                .where(t.approvals.c.id == approval_id, t.approvals.c.decision == "pending")
                .values(
                    decision=decision,
                    decided_by=by,
                    edited_args=edited_args,
                    comment=comment,
                    decided_at=utcnow(),
                )
            )
        return res.rowcount == 1

    async def count_pending_approvals(self) -> int:
        q = (
            sa.select(sa.func.count())
            .select_from(t.approvals)
            .where(t.approvals.c.decision == "pending")
        )
        async with self.engine.connect() as c:
            return int((await c.execute(q)).scalar_one())

    # -- events (for SSE replay) --------------------------------------------------------

    async def append_event(self, run_id: str, type_: str, data: dict[str, Any]) -> int:
        for _ in range(5):
            async with self.engine.begin() as c:
                seq = (
                    await c.execute(
                        sa.select(sa.func.coalesce(sa.func.max(t.run_events.c.seq), 0)).where(
                            t.run_events.c.run_id == run_id
                        )
                    )
                ).scalar_one() + 1
                try:
                    await c.execute(
                        t.run_events.insert().values(
                            run_id=run_id, seq=seq, type=type_, data=data, created_at=utcnow()
                        )
                    )
                except IntegrityError:
                    continue  # a concurrent writer took this seq; retry
            return int(seq)
        raise RuntimeError("could not allocate event sequence")

    async def list_events(self, run_id: str, after_seq: int = 0) -> list[dict[str, Any]]:
        q = (
            sa.select(t.run_events.c.seq, t.run_events.c.type, t.run_events.c.data)
            .where(t.run_events.c.run_id == run_id, t.run_events.c.seq > after_seq)
            .order_by(t.run_events.c.seq)
        )
        async with self.engine.connect() as c:
            return [
                {"seq": r["seq"], "type": r["type"], "data": r["data"]}
                for r in (await c.execute(q)).mappings()
            ]

    # -- spans ---------------------------------------------------------------------------

    async def insert_span(self, span: dict[str, Any]) -> None:
        async with self.engine.begin() as c:
            await c.execute(t.spans.insert().values(**span))

    async def list_spans(self, run_id: str) -> list[dict[str, Any]]:
        q = sa.select(t.spans).where(t.spans.c.run_id == run_id).order_by(t.spans.c.started_at)
        async with self.engine.connect() as c:
            return [dict(r) for r in (await c.execute(q)).mappings()]

    # -- audit ---------------------------------------------------------------------------

    async def audit(
        self,
        tenant_id: str,
        actor: str,
        actor_id: str,
        action: str,
        target: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        async with self.engine.begin() as c:
            await c.execute(
                t.audit_log.insert().values(
                    tenant_id=tenant_id,
                    actor=actor,
                    actor_id=actor_id,
                    action=action,
                    target=target,
                    details=details or {},
                    created_at=utcnow(),
                )
            )

    async def list_audit(self, tenant_id: str, limit: int = 200) -> list[dict[str, Any]]:
        q = (
            sa.select(t.audit_log)
            .where(t.audit_log.c.tenant_id == tenant_id)
            .order_by(t.audit_log.c.id)
            .limit(limit)
        )
        async with self.engine.connect() as c:
            return [dict(r) for r in (await c.execute(q)).mappings()]

    # -- notes (long-term memory backend) ---------------------------------------------------

    async def put(self, tenant_id: str, user_id: str, key: str, value: str) -> None:
        async with self.engine.begin() as c:
            res = await c.execute(
                t.memory_notes.update()
                .where(
                    t.memory_notes.c.tenant_id == tenant_id,
                    t.memory_notes.c.user_id == user_id,
                    t.memory_notes.c.key == key,
                )
                .values(value=value, updated_at=utcnow())
            )
            if res.rowcount == 0:
                await c.execute(
                    t.memory_notes.insert().values(
                        tenant_id=tenant_id,
                        user_id=user_id,
                        key=key,
                        value=value,
                        updated_at=utcnow(),
                    )
                )

    async def get(self, tenant_id: str, user_id: str, key: str) -> str | None:
        q = sa.select(t.memory_notes.c.value).where(
            t.memory_notes.c.tenant_id == tenant_id,
            t.memory_notes.c.user_id == user_id,
            t.memory_notes.c.key == key,
        )
        async with self.engine.connect() as c:
            value = (await c.execute(q)).scalar_one_or_none()
        return None if value is None else str(value)
