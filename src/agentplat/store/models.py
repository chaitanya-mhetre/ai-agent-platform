from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from agentplat.messages import Message
from agentplat.state import RunStatus


@dataclass(slots=True)
class Agent:
    id: str
    tenant_id: str
    name: str
    system_prompt: str
    allowed_tools: list[str]
    model_config: dict[str, Any] = field(default_factory=dict)
    max_steps: int = 8
    max_cost_usd: float = 0.50
    max_tokens: int = 50_000
    version: int = 1


@dataclass(slots=True)
class Run:
    id: str
    tenant_id: str
    agent_id: str
    agent_version: int
    user_id: str
    status: RunStatus
    input: str
    messages: list[Message]
    final_output: str | None = None
    tainted: bool = False
    step_count: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    est_cost_usd: float = 0.0
    cancel_requested: bool = False
    error: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None

    def summary(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "agent_id": self.agent_id,
            "status": self.status.value,
            "input": self.input,
            "final_output": self.final_output,
            "tainted": self.tainted,
            "step_count": self.step_count,
            "usage": {
                "prompt_tokens": self.prompt_tokens,
                "completion_tokens": self.completion_tokens,
                "est_cost_usd": round(self.est_cost_usd, 6),
            },
            "error": self.error,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


@dataclass(slots=True)
class ToolCallRecord:
    id: str
    run_id: str
    step: int
    tool_name: str
    args: dict[str, Any]
    idempotency_key: str
    decision: str
    status: str
    decision_reason: str | None = None
    output: Any = None
    output_tainted: bool = False
    latency_ms: float | None = None
    attempts: int = 0
    error: str | None = None


@dataclass(slots=True)
class Approval:
    id: str
    tenant_id: str
    run_id: str
    tool_call_id: str
    tool_name: str
    args: dict[str, Any]
    reason: str
    decision: str = "pending"
    edited_args: dict[str, Any] | None = None
    comment: str | None = None
    decided_by: str | None = None
    requested_at: datetime | None = None
    decided_at: datetime | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "run_id": self.run_id,
            "tool_call_id": self.tool_call_id,
            "tool_name": self.tool_name,
            "args": self.args,
            "reason": self.reason,
            "decision": self.decision,
            "edited_args": self.edited_args,
            "comment": self.comment,
            "decided_by": self.decided_by,
            "requested_at": self.requested_at.isoformat() if self.requested_at else None,
            "decided_at": self.decided_at.isoformat() if self.decided_at else None,
        }
