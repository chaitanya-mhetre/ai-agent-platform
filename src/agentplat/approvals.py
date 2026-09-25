"""Human-in-the-loop decisions, and resuming the paused run afterwards."""

from __future__ import annotations

from typing import Any

from agentplat.container import Container
from agentplat.state import RunStatus
from agentplat.store.models import Approval


class ApprovalError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


DECIDE_PERMISSION = "approvals:decide"


async def decide(
    c: Container,
    *,
    tenant_id: str,
    user_id: str,
    approval_id: str,
    decision: str,
    edited_args: dict[str, Any] | None = None,
    comment: str | None = None,
) -> Approval:
    if DECIDE_PERMISSION not in await c.store.permissions(tenant_id, user_id):
        raise ApprovalError(403, f"requires permission {DECIDE_PERMISSION}")
    approval = await c.store.get_approval(approval_id, tenant_id)
    if approval.decision != "pending":
        raise ApprovalError(409, f"approval already {approval.decision}")
    stored = {"approve": "approved", "reject": "rejected", "edit": "edited"}[decision]
    if stored == "edited" and not edited_args:
        raise ApprovalError(422, "edit requires edited_args")

    if not await c.store.decide_approval(
        approval_id, stored, by=user_id, edited_args=edited_args, comment=comment
    ):
        raise ApprovalError(409, "approval was decided concurrently")
    await c.store.audit(
        tenant_id,
        "user",
        user_id,
        f"approval.{stored}",
        approval.tool_name,
        {"approval_id": approval_id, "run_id": approval.run_id, "edited_args": edited_args},
    )
    await c.runtime.events.emit(
        approval.run_id, "approval_decided", approval_id=approval_id, decision=stored, by=user_id
    )

    # Resume once nothing else in the run is waiting on a human.
    if await c.store.pending_approvals_for_run(approval.run_id) == 0 and await c.store.transition(
        approval.run_id, RunStatus.AWAITING_APPROVAL, RunStatus.QUEUED
    ):
        await c.runtime.events.emit(approval.run_id, "status", status=RunStatus.QUEUED.value)
        await c.queue.enqueue(approval.run_id)
    return await c.store.get_approval(approval_id)
