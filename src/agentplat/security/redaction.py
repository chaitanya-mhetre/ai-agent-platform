"""Keep secrets out of prompts, traces, events, logs and the audit trail."""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any

DEFAULT_PATTERNS = [
    re.compile(r"sk-[A-Za-z0-9_\-]{16,}"),  # OpenAI-style keys
    re.compile(r"sk-ant-[A-Za-z0-9_\-]{16,}"),
    re.compile(r"AIza[0-9A-Za-z_\-]{30,}"),  # Google API keys
    re.compile(r"(?i)bearer\s+[A-Za-z0-9._\-]{16,}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]+?-----END [A-Z ]*PRIVATE KEY-----"),
]
MASK = "[REDACTED]"


class Redactor:
    def __init__(
        self, secrets: Iterable[str] = (), patterns: Iterable[re.Pattern[str]] = ()
    ) -> None:
        self._literals = sorted({s for s in secrets if len(s) >= 6}, key=len, reverse=True)
        self._patterns = [*DEFAULT_PATTERNS, *patterns]

    def add_secret(self, value: str) -> None:
        if len(value) >= 6 and value not in self._literals:
            self._literals.append(value)
            self._literals.sort(key=len, reverse=True)

    def text(self, value: str) -> str:
        for lit in self._literals:
            value = value.replace(lit, MASK)
        for pat in self._patterns:
            value = pat.sub(MASK, value)
        return value

    def deep(self, value: Any) -> Any:
        if isinstance(value, str):
            return self.text(value)
        if isinstance(value, dict):
            return {k: self.deep(v) for k, v in value.items()}
        if isinstance(value, list | tuple):
            return [self.deep(v) for v in value]
        return value


class SecretStore:
    """Server-side credentials, scoped per tool. They reach a tool through its
    ToolContext and are never placed in prompts, tool arguments or traces."""

    def __init__(self, by_tool: dict[str, dict[str, str]] | None = None) -> None:
        self._by_tool = by_tool or {}

    def for_tool(self, tool_name: str) -> dict[str, str]:
        return dict(self._by_tool.get(tool_name, {}))

    def all_values(self) -> list[str]:
        return [v for secrets in self._by_tool.values() for v in secrets.values()]
