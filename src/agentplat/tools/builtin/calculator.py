"""Arithmetic without eval(): parse to an AST and walk only whitelisted nodes."""

from __future__ import annotations

import ast
import operator
from collections.abc import Callable
from typing import ClassVar

from pydantic import Field

from agentplat.tools.base import Tool, ToolArgs, ToolContext, ToolError, ToolResult

_BIN: dict[type[ast.operator], Callable[[float, float], float]] = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}
_UNARY: dict[type[ast.unaryop], Callable[[float], float]] = {
    ast.USub: operator.neg,
    ast.UAdd: operator.pos,
}
MAX_EXPONENT = 100


def safe_eval(expression: str) -> float:
    try:
        tree = ast.parse(expression, mode="eval")
    except SyntaxError as exc:
        raise ToolError(f"invalid expression: {expression!r}") from exc
    return _eval(tree.body)


def _eval(node: ast.expr) -> float:
    if isinstance(node, ast.Constant) and isinstance(node.value, int | float):
        return float(node.value)
    if isinstance(node, ast.BinOp) and type(node.op) in _BIN:
        left, right = _eval(node.left), _eval(node.right)
        if isinstance(node.op, ast.Pow) and abs(right) > MAX_EXPONENT:
            raise ToolError("exponent too large")
        try:
            return _BIN[type(node.op)](left, right)
        except ZeroDivisionError as exc:
            raise ToolError("division by zero") from exc
    if isinstance(node, ast.UnaryOp) and type(node.op) in _UNARY:
        return _UNARY[type(node.op)](_eval(node.operand))
    raise ToolError(f"unsupported syntax: {ast.dump(node)[:60]}")


class CalculatorArgs(ToolArgs):
    expression: str = Field(max_length=200, description="Arithmetic, e.g. '0.17 * 2340 + 12'")


class Calculator(Tool[CalculatorArgs]):
    name: ClassVar[str] = "calculator"
    description: ClassVar[str] = (
        "Evaluate an arithmetic expression (+ - * / // % ** and parentheses)."
    )
    args_model = CalculatorArgs

    async def run(self, args: CalculatorArgs, ctx: ToolContext) -> ToolResult:
        value = safe_eval(args.expression)
        return ToolResult({"expression": args.expression, "result": round(value, 10)})
