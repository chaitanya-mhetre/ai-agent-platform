"""Long-term memory v1: a per-user key/value notes store."""

from __future__ import annotations

from typing import ClassVar, Protocol

from pydantic import Field

from agentplat.tools.base import RiskLevel, Tool, ToolArgs, ToolContext, ToolResult


class NotesBackend(Protocol):
    async def put(self, tenant_id: str, user_id: str, key: str, value: str) -> None: ...
    async def get(self, tenant_id: str, user_id: str, key: str) -> str | None: ...


class InMemoryNotes:
    def __init__(self) -> None:
        self.data: dict[tuple[str, str, str], str] = {}

    async def put(self, tenant_id: str, user_id: str, key: str, value: str) -> None:
        self.data[(tenant_id, user_id, key)] = value

    async def get(self, tenant_id: str, user_id: str, key: str) -> str | None:
        return self.data.get((tenant_id, user_id, key))


class NotesWriteArgs(ToolArgs):
    key: str = Field(min_length=1, max_length=100)
    value: str = Field(max_length=2000)


class NotesReadArgs(ToolArgs):
    key: str = Field(min_length=1, max_length=100)


class NotesWrite(Tool[NotesWriteArgs]):
    name: ClassVar[str] = "notes_write"
    description: ClassVar[str] = "Save a note for this user under a key (overwrites)."
    args_model = NotesWriteArgs
    risk_level = RiskLevel.WRITE
    required_permissions = frozenset({"notes:write"})

    def __init__(self, backend: NotesBackend) -> None:
        self.backend = backend

    async def run(self, args: NotesWriteArgs, ctx: ToolContext) -> ToolResult:
        await self.backend.put(ctx.tenant_id, ctx.user_id, args.key, args.value)
        return ToolResult({"saved": args.key})


class NotesRead(Tool[NotesReadArgs]):
    name: ClassVar[str] = "notes_read"
    description: ClassVar[str] = "Read a previously saved note by key."
    args_model = NotesReadArgs
    required_permissions = frozenset({"notes:read"})

    def __init__(self, backend: NotesBackend) -> None:
        self.backend = backend

    async def run(self, args: NotesReadArgs, ctx: ToolContext) -> ToolResult:
        value = await self.backend.get(ctx.tenant_id, ctx.user_id, args.key)
        return ToolResult({"key": args.key, "value": value, "found": value is not None})
