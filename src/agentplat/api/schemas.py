from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class AgentIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    system_prompt: str = Field(min_length=1, max_length=20_000)
    allowed_tools: list[str] = Field(default_factory=list)
    model_config_: dict[str, Any] = Field(default_factory=dict, alias="model_config")
    max_steps: int = Field(default=8, ge=1, le=50)
    max_cost_usd: float = Field(default=0.5, gt=0, le=100)
    max_tokens: int = Field(default=50_000, ge=100, le=2_000_000)


class RunIn(BaseModel):
    agent_id: str
    input: str = Field(min_length=1, max_length=20_000)
    mode: Literal["sync", "background"] = "background"


class PermissionsIn(BaseModel):
    permissions: list[str] = Field(min_length=1)


class ApprovalDecisionIn(BaseModel):
    decision: Literal["approve", "reject", "edit"]
    edited_args: dict[str, Any] | None = None
    comment: str | None = Field(default=None, max_length=2000)
