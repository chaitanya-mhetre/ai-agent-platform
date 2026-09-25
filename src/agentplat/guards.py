"""The guard pipeline: decides whether a proposed tool call may run.

The model's tool call is a *proposal*. Before anything executes, it passes:

    1. schema validation      - args must match the tool's JSON Schema exactly
    2. authorization          - tool on the agent's allow-list AND the end user
                                holds every permission the tool requires
    3. policy                 - risk level, taint, loop detection
    4. approval gate          - destructive/risky calls wait for a human

Authorization happens here, at execution time, against the *end user's*
permissions. Listing a tool in the prompt is only a hint; this is the check that
actually enforces security, so a model can never use privileges it wasn't given.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from agentplat.messages import Role, ToolCall
from agentplat.store.models import Agent, Approval, Run
from agentplat.tools.base import RiskLevel, Tool, ToolArgs


class Verdict(StrEnum):
    ALLOW = "allowed"
    DENY = "denied"
    APPROVAL = "needs_approval"


@dataclass(frozen=True, slots=True)
class Decision:
    verdict: Verdict
    reason: str = ""
    args: ToolArgs | None = None  # validated args (set unless DENY)
    arguments: dict[str, Any] = field(default_factory=dict)  # the args that will run
    approval: Approval | None = None


class AllowListGuard:
    """Minimal guard: agent allow-list + schema validation only."""

    async def decide(self, run: Run, agent: Agent, call: ToolCall, tool: Tool[Any]) -> Decision:
        if call.name not in agent.allowed_tools:
            return Decision(Verdict.DENY, f"tool {call.name!r} is not available to this agent")
        return Decision(Verdict.ALLOW, "allowed", tool.parse_args(call.arguments), call.arguments)


PermissionLookup = Callable[[str, str], Awaitable[frozenset[str]]]
ApprovalLookup = Callable[[str, str], Awaitable[Approval | None]]


@dataclass(slots=True)
class PolicyConfig:
    # Once a run has read untrusted content, these risk levels need a human.
    taint_escalates: frozenset[RiskLevel] = frozenset(
        {RiskLevel.EXTERNAL, RiskLevel.WRITE, RiskLevel.DESTRUCTIVE}
    )
    always_approve: frozenset[RiskLevel] = frozenset({RiskLevel.DESTRUCTIVE})
    max_identical_calls: int = 3


class GuardPipeline:
    def __init__(
        self,
        permissions: PermissionLookup,
        approvals: ApprovalLookup,
        policy: PolicyConfig | None = None,
    ) -> None:
        self._permissions = permissions
        self._approvals = approvals
        self.policy = policy or PolicyConfig()

    async def decide(self, run: Run, agent: Agent, call: ToolCall, tool: Tool[Any]) -> Decision:
        approval = await self._approvals(run.id, call.id)
        arguments = call.arguments
        if approval is not None and approval.decision == "rejected":
            note = f": {approval.comment}" if approval.comment else ""
            return Decision(Verdict.DENY, f"rejected by a human reviewer{note}", approval=approval)
        if approval is not None and approval.decision == "edited" and approval.edited_args:
            arguments = approval.edited_args  # re-validated and re-authorized below

        # 1. schema (raises pydantic.ValidationError -> "invalid arguments")
        args = tool.parse_args(arguments)

        # 2. authorization
        if call.name not in agent.allowed_tools:
            return Decision(Verdict.DENY, f"tool {call.name!r} is not available to this agent")
        held = await self._permissions(run.tenant_id, run.user_id)
        missing = sorted(tool.required_permissions - held)
        if missing:
            return Decision(Verdict.DENY, f"permission denied: user lacks {', '.join(missing)}")

        # 3. policy
        if self._identical_calls(run, call.name, arguments) > self.policy.max_identical_calls:
            return Decision(
                Verdict.DENY,
                "repeated identical call blocked; try a different approach or answer",
            )
        needs, why = self._needs_approval(run, tool)

        # 4. approval gate
        if needs:
            if approval is None or approval.decision == "pending":
                return Decision(Verdict.APPROVAL, why, args, arguments, approval)
            return Decision(
                Verdict.ALLOW, f"approved by {approval.decided_by}", args, arguments, approval
            )
        return Decision(Verdict.ALLOW, "allowed", args, arguments, approval)

    def _needs_approval(self, run: Run, tool: Tool[Any]) -> tuple[bool, str]:
        if tool.risk_level in self.policy.always_approve:
            return True, f"{tool.risk_level.value} tools always require approval"
        if tool.requires_approval:
            return True, f"tool {tool.name!r} requires approval"
        if run.tainted and tool.risk_level in self.policy.taint_escalates:
            return True, (
                "run has read untrusted content; "
                f"{tool.risk_level.value} actions now require approval"
            )
        return False, ""

    @staticmethod
    def _identical_calls(run: Run, name: str, arguments: dict[str, Any]) -> int:
        target = json.dumps(arguments, sort_keys=True, default=str)
        return sum(
            1
            for m in run.messages
            if m.role is Role.ASSISTANT
            for c in m.tool_calls
            if c.name == name and json.dumps(c.arguments, sort_keys=True, default=str) == target
        )
