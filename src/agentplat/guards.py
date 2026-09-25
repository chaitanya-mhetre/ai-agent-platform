"""Decides whether a proposed tool call may run. (Extended in M4.)"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from agentplat.messages import ToolCall
from agentplat.store.models import Agent, Run
from agentplat.tools.base import Tool


class Verdict(StrEnum):
    ALLOW = "allowed"
    DENY = "denied"
    APPROVAL = "needs_approval"


@dataclass(frozen=True, slots=True)
class Decision:
    verdict: Verdict
    reason: str = ""
    args: Any = None  # validated args model when verdict != DENY


class AllowListGuard:
    async def decide(self, run: Run, agent: Agent, call: ToolCall, tool: Tool[Any]) -> Decision:
        if call.name not in agent.allowed_tools:
            return Decision(Verdict.DENY, f"tool {call.name!r} is not available to this agent")
        return Decision(Verdict.ALLOW, "allowed", tool.parse_args(call.arguments))
