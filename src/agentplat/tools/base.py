"""The Tool abstraction.

A tool declares *what it needs* (permissions, approval, risk) as data. The
runtime enforces those declarations; the tool body only does the work.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict

from agentplat.messages import ToolSchema


class RiskLevel(StrEnum):
    READ = "read"
    WRITE = "write"
    DESTRUCTIVE = "destructive"
    EXTERNAL = "external"  # reaches outside the system (network, email, notifications)


class ToolArgs(BaseModel):
    """Base for tool argument models. Unknown fields are rejected."""

    model_config = ConfigDict(extra="forbid")


class ToolError(Exception):
    """A tool failed. Only `transient` failures are retried."""

    def __init__(self, message: str, *, transient: bool = False):
        super().__init__(message)
        self.transient = transient


@dataclass(frozen=True, slots=True)
class ToolContext:
    run_id: str
    user_id: str
    tenant_id: str
    idempotency_key: str
    secrets: dict[str, str] = field(default_factory=dict)  # only this tool's secrets


@dataclass(slots=True)
class ToolResult:
    output: Any
    # True when the output contains content from an untrusted source (web page,
    # uploaded file, ...). Untrusted content may carry prompt-injection payloads.
    tainted: bool = False
    source: str | None = None


class Tool[A: ToolArgs](ABC):
    name: ClassVar[str]
    description: ClassVar[str]
    args_model: ClassVar[type[ToolArgs]]
    risk_level: ClassVar[RiskLevel] = RiskLevel.READ
    required_permissions: ClassVar[frozenset[str]] = frozenset()
    requires_approval: ClassVar[bool] = False
    timeout_s: ClassVar[float] = 10.0
    max_retries: ClassVar[int] = 0
    idempotent: ClassVar[bool] = True
    output_model: ClassVar[type[BaseModel] | None] = None

    @abstractmethod
    async def run(self, args: A, ctx: ToolContext) -> ToolResult: ...

    def parse_args(self, raw: dict[str, Any]) -> A:
        return self.args_model.model_validate(raw)  # type: ignore[return-value]

    def schema(self) -> ToolSchema:
        params = self.args_model.model_json_schema()
        params.setdefault("additionalProperties", False)
        params.pop("title", None)
        return ToolSchema(self.name, self.description, params)

    def describe(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "args_schema": self.schema().parameters,
            "risk_level": self.risk_level.value,
            "required_permissions": sorted(self.required_permissions),
            "requires_approval": self.requires_approval,
            "timeout_s": self.timeout_s,
            "max_retries": self.max_retries,
            "idempotent": self.idempotent,
        }
