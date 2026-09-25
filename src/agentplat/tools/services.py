"""Fake downstream services used by the built-in demo tools.

They behave like real services in the ways that matter for the runtime:
writes dedupe on the idempotency key, so a retried call never double-sends.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class FakeOutbox:
    sent: dict[str, dict[str, str]] = field(default_factory=dict)

    def send(self, idempotency_key: str, recipient: str, message: str) -> tuple[str, bool]:
        """Returns (message_id, duplicate)."""
        if idempotency_key in self.sent:
            return self.sent[idempotency_key]["id"], True
        msg_id = f"msg_{len(self.sent) + 1}"
        self.sent[idempotency_key] = {"id": msg_id, "recipient": recipient, "message": message}
        return msg_id, False


@dataclass
class FakeCalendar:
    events: dict[str, dict[str, Any]] = field(default_factory=dict)

    def create(self, idempotency_key: str, event: dict[str, Any]) -> str:
        if idempotency_key not in self.events:
            self.events[idempotency_key] = {"id": f"evt_{len(self.events) + 1}", **event}
        return str(self.events[idempotency_key]["id"])


@dataclass
class FakeRecords:
    tables: dict[str, list[dict[str, Any]]] = field(
        default_factory=lambda: {
            "orders": [{"id": i, "amount": 100 * i} for i in range(1, 6)],
            "customers": [{"id": i, "name": f"customer-{i}"} for i in range(1, 4)],
        }
    )

    def delete_all(self, table: str) -> int:
        rows = self.tables.get(table, [])
        n = len(rows)
        self.tables[table] = []
        return n


@dataclass
class Services:
    outbox: FakeOutbox = field(default_factory=FakeOutbox)
    calendar: FakeCalendar = field(default_factory=FakeCalendar)
    records: FakeRecords = field(default_factory=FakeRecords)
