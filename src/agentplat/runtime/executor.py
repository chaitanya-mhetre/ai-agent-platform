"""Runs one tool call with a timeout and bounded retries.

Failure handling lives here so the agent loop stays about agent logic. Only
transient failures (timeouts, ToolError(transient=True)) are retried; bad
arguments or permission problems never are.
"""

from __future__ import annotations

import asyncio
import random
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from agentplat.tools.base import Tool, ToolArgs, ToolContext, ToolError, ToolResult


@dataclass(slots=True)
class ExecOutcome:
    result: ToolResult | None
    error: str | None
    attempts: int
    latency_ms: float
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return self.result is not None


class ToolExecutor:
    def __init__(
        self,
        *,
        base_delay_s: float = 0.2,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.base_delay_s = base_delay_s
        self._sleep = sleep

    async def execute(self, tool: Tool[Any], args: ToolArgs, ctx: ToolContext) -> ExecOutcome:
        start = time.perf_counter()
        attempts = 0
        max_attempts = 1 + max(0, tool.max_retries)
        last_error = "unknown error"
        timed_out = False
        while attempts < max_attempts:
            attempts += 1
            try:
                async with asyncio.timeout(tool.timeout_s):
                    result = await tool.run(args, ctx)
                return ExecOutcome(result, None, attempts, _ms(start))
            except TimeoutError:
                timed_out = True
                last_error = f"tool timed out after {tool.timeout_s}s"
            except ToolError as exc:
                last_error = str(exc)
                if not exc.transient:
                    break
            except Exception as exc:  # a buggy tool must not crash the run
                last_error = f"tool crashed: {type(exc).__name__}: {exc}"
                break
            if attempts < max_attempts:
                await self._sleep(random.uniform(0, self.base_delay_s * 2 ** (attempts - 1)))
        return ExecOutcome(None, last_error, attempts, _ms(start), timed_out)


def _ms(start: float) -> float:
    return round((time.perf_counter() - start) * 1000, 3)
