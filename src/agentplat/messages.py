"""Provider-neutral message types.

Every provider adapter translates to and from these, so the orchestrator never
sees an OpenAI- or Gemini-specific payload.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class Role(StrEnum):
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


@dataclass(frozen=True, slots=True)
class ToolCall:
    """A tool call *proposed* by the model. Nothing has executed yet."""

    id: str
    name: str
    arguments: dict[str, Any]


@dataclass(slots=True)
class Message:
    role: Role
    content: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    tool_call_id: str | None = None  # set on Role.TOOL messages
    name: str | None = None  # tool name on Role.TOOL messages

    def to_dict(self) -> dict[str, Any]:
        return {
            "role": self.role.value,
            "content": self.content,
            "tool_calls": [
                {"id": c.id, "name": c.name, "arguments": c.arguments} for c in self.tool_calls
            ],
            "tool_call_id": self.tool_call_id,
            "name": self.name,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Message:
        return cls(
            role=Role(data["role"]),
            content=data.get("content") or "",
            tool_calls=[ToolCall(**c) for c in data.get("tool_calls") or []],
            tool_call_id=data.get("tool_call_id"),
            name=data.get("name"),
        )


@dataclass(frozen=True, slots=True)
class Usage:
    prompt_tokens: int = 0
    completion_tokens: int = 0

    def __add__(self, other: Usage) -> Usage:
        return Usage(
            self.prompt_tokens + other.prompt_tokens,
            self.completion_tokens + other.completion_tokens,
        )


@dataclass(frozen=True, slots=True)
class ToolSchema:
    """What the model is told about a tool (name, description, JSON Schema of args)."""

    name: str
    description: str
    parameters: dict[str, Any]


@dataclass(slots=True)
class ModelResponse:
    content: str | None
    tool_calls: list[ToolCall]
    usage: Usage
    model: str
    finish_reason: str = "stop"

    @property
    def is_final(self) -> bool:
        return not self.tool_calls
