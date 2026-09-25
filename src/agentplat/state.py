"""Run lifecycle as an explicit state machine.

Explicit states make crashes recoverable (a worker knows exactly where a run
stopped) and make illegal jumps (e.g. succeeded -> running) impossible.
"""

from __future__ import annotations

from enum import StrEnum


class RunStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    AWAITING_APPROVAL = "awaiting_approval"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    BUDGET_EXCEEDED = "budget_exceeded"
    MAX_STEPS = "max_steps_exceeded"

    @property
    def is_terminal(self) -> bool:
        return self in _TERMINAL


_TERMINAL = frozenset(
    {
        RunStatus.SUCCEEDED,
        RunStatus.FAILED,
        RunStatus.CANCELLED,
        RunStatus.BUDGET_EXCEEDED,
        RunStatus.MAX_STEPS,
    }
)

TRANSITIONS: dict[RunStatus, frozenset[RunStatus]] = {
    RunStatus.QUEUED: frozenset({RunStatus.RUNNING, RunStatus.CANCELLED}),
    RunStatus.RUNNING: frozenset(
        {
            RunStatus.RUNNING,  # a reclaimed run is re-entered by another worker
            RunStatus.AWAITING_APPROVAL,
            RunStatus.QUEUED,
            *_TERMINAL,
        }
    ),
    RunStatus.AWAITING_APPROVAL: frozenset({RunStatus.QUEUED, RunStatus.CANCELLED}),
    **{s: frozenset() for s in _TERMINAL},
}


class IllegalTransitionError(Exception):
    def __init__(self, current: RunStatus, target: RunStatus) -> None:
        super().__init__(f"illegal run transition {current.value} -> {target.value}")
        self.current = current
        self.target = target


def check_transition(current: RunStatus, target: RunStatus) -> None:
    if target not in TRANSITIONS[current]:
        raise IllegalTransitionError(current, target)
