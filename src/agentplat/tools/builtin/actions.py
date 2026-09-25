"""Tools with side effects: notifications, calendar, destructive deletes."""

from __future__ import annotations

from datetime import datetime
from typing import ClassVar, Literal

from pydantic import Field

from agentplat.tools.base import RiskLevel, Tool, ToolArgs, ToolContext, ToolError, ToolResult
from agentplat.tools.services import FakeCalendar, FakeOutbox, FakeRecords


class SendNotificationArgs(ToolArgs):
    recipient: str = Field(min_length=1, max_length=100)
    message: str = Field(min_length=1, max_length=1000)


class SendNotification(Tool[SendNotificationArgs]):
    name: ClassVar[str] = "send_notification"
    description: ClassVar[str] = "Send a notification message to a person or team."
    args_model = SendNotificationArgs
    risk_level = RiskLevel.EXTERNAL
    required_permissions = frozenset({"notify:send"})
    requires_approval = True
    max_retries = 2
    idempotent = False  # made safe by the idempotency key

    def __init__(self, outbox: FakeOutbox) -> None:
        self.outbox = outbox

    async def run(self, args: SendNotificationArgs, ctx: ToolContext) -> ToolResult:
        msg_id, dup = self.outbox.send(ctx.idempotency_key, args.recipient, args.message)
        return ToolResult({"message_id": msg_id, "duplicate": dup})


class CalendarArgs(ToolArgs):
    title: str = Field(min_length=1, max_length=200)
    start: datetime
    duration_minutes: int = Field(ge=5, le=480)


class CalendarCreateEvent(Tool[CalendarArgs]):
    name: ClassVar[str] = "calendar_create_event"
    description: ClassVar[str] = "Create a calendar event for the user."
    args_model = CalendarArgs
    risk_level = RiskLevel.WRITE
    required_permissions = frozenset({"calendar:write"})

    def __init__(self, calendar: FakeCalendar) -> None:
        self.calendar = calendar

    async def run(self, args: CalendarArgs, ctx: ToolContext) -> ToolResult:
        event_id = self.calendar.create(
            ctx.idempotency_key,
            {
                "title": args.title,
                "start": args.start.isoformat(),
                "minutes": args.duration_minutes,
            },
        )
        return ToolResult({"event_id": event_id})


class DeleteRecordsArgs(ToolArgs):
    table: Literal["orders", "customers"]
    where: str = Field(max_length=200, description="Filter; only '1=1' (all rows) is supported")


class DeleteRecords(Tool[DeleteRecordsArgs]):
    name: ClassVar[str] = "delete_records"
    description: ClassVar[str] = "Delete records from a table. Irreversible."
    args_model = DeleteRecordsArgs
    risk_level = RiskLevel.DESTRUCTIVE
    required_permissions = frozenset({"records:delete"})

    def __init__(self, records: FakeRecords) -> None:
        self.records = records

    async def run(self, args: DeleteRecordsArgs, ctx: ToolContext) -> ToolResult:
        if args.where.strip() != "1=1":
            raise ToolError("only full-table deletes are supported in the demo")
        return ToolResult({"deleted": self.records.delete_all(args.table)})
