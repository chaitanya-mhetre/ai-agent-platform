import asyncio
from typing import ClassVar

from agentplat.runtime.executor import ToolExecutor
from agentplat.tools.base import Tool, ToolArgs, ToolContext, ToolError, ToolResult

CTX = ToolContext("r", "u", "t", "k")


class NoArgs(ToolArgs):
    pass


class Flaky(Tool[NoArgs]):
    name: ClassVar[str] = "flaky"
    description: ClassVar[str] = "x"
    args_model = NoArgs
    max_retries = 2

    def __init__(self, failures: list[Exception]) -> None:
        self.failures = failures
        self.calls = 0

    async def run(self, args: NoArgs, ctx: ToolContext) -> ToolResult:
        self.calls += 1
        if self.failures:
            raise self.failures.pop(0)
        return ToolResult("ok")


class Slow(Tool[NoArgs]):
    name: ClassVar[str] = "slow"
    description: ClassVar[str] = "x"
    args_model = NoArgs
    timeout_s = 0.05

    async def run(self, args: NoArgs, ctx: ToolContext) -> ToolResult:
        await asyncio.sleep(1)
        return ToolResult("late")


async def _no_sleep(_: float) -> None:
    return None


async def test_transient_errors_retried_until_success() -> None:
    tool = Flaky([ToolError("503", transient=True), ToolError("503", transient=True)])
    out = await ToolExecutor(sleep=_no_sleep).execute(tool, NoArgs(), CTX)
    assert out.ok and out.attempts == 3


async def test_permanent_error_not_retried() -> None:
    tool = Flaky([ToolError("bad input")])
    out = await ToolExecutor(sleep=_no_sleep).execute(tool, NoArgs(), CTX)
    assert not out.ok and out.attempts == 1 and out.error == "bad input"


async def test_timeout() -> None:
    out = await ToolExecutor(sleep=_no_sleep).execute(Slow(), NoArgs(), CTX)
    assert out.timed_out and not out.ok and "timed out" in (out.error or "")


async def test_buggy_tool_does_not_crash_runtime() -> None:
    tool = Flaky([KeyError("oops")])
    out = await ToolExecutor(sleep=_no_sleep).execute(tool, NoArgs(), CTX)
    assert not out.ok and "KeyError" in (out.error or "")
