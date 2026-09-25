from __future__ import annotations

from collections.abc import Iterable, Iterator
from typing import Any

from agentplat.messages import ToolSchema
from agentplat.tools.base import Tool


class UnknownToolError(KeyError):
    pass


class ToolRegistry:
    def __init__(self, tools: Iterable[Tool[Any]] = ()) -> None:
        self._tools: dict[str, Tool[Any]] = {}
        for tool in tools:
            self.register(tool)

    def register(self, tool: Tool[Any]) -> None:
        if tool.name in self._tools:
            raise ValueError(f"tool {tool.name!r} already registered")
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool[Any]:
        try:
            return self._tools[name]
        except KeyError:
            raise UnknownToolError(name) from None

    def __contains__(self, name: object) -> bool:
        return name in self._tools

    def __iter__(self) -> Iterator[Tool[Any]]:
        return iter(self._tools.values())

    def schemas(self, names: Iterable[str] | None = None) -> list[ToolSchema]:
        selected = self._tools.values() if names is None else [self.get(n) for n in names]
        return [t.schema() for t in selected]
