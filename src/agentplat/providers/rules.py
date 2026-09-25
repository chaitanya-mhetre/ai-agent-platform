"""A deterministic, offline, keyword-driven "model".

Used for local dev (no API key needed) and as the *offline scripted baseline*
for agent-eval. It is intentionally naive, and in one way intentionally unsafe:
it obeys instructions it finds inside tool outputs, the way a gullible LLM might.
That lets the eval suite show whether the runtime's policies stop an injected
action, independent of how smart the model is. Its scores say nothing about the
quality of any real model.
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from typing import Any

from agentplat.messages import Message, ModelResponse, Role, ToolCall, ToolSchema, Usage

_URL = re.compile(r"https?://[^\s\"'<>]+")
_PERCENT_OF = re.compile(r"(\d+(?:\.\d+)?)\s*%\s*of\s*([\d,]+(?:\.\d+)?)", re.I)
_ARITH = re.compile(r"[\d.,\s()+\-*/%]*\d[\d.,\s()+\-*/%]*[+\-*/][\d.,\s()+\-*/%]*\d")
_INJECTED_ACTION = re.compile(r"CALL\s+(\w+)\s*(\{.*?\})", re.S)


def _approx_tokens(text: str) -> int:
    return max(1, len(text) // 4)


class RuleBasedProvider:
    name = "rules"

    async def complete(
        self, messages: Sequence[Message], tools: Sequence[ToolSchema], *, model: str | None = None
    ) -> ModelResponse:
        available = {t.name for t in tools}
        prompt_tokens = sum(_approx_tokens(m.content) for m in messages)
        last = messages[-1]

        if last.role is Role.TOOL:
            injected = self._injected_call(last, available, messages)
            if injected:
                return self._call(injected, prompt_tokens)
            return self._final(self._summarise(messages), prompt_tokens)

        user = next(m.content for m in reversed(messages) if m.role is Role.USER)
        call = self._plan(user, available)
        if call is None:
            return self._final(f"I can answer directly: {user[:200]}", prompt_tokens)
        return self._call(call, prompt_tokens)

    # -- planning ---------------------------------------------------------------

    def _plan(self, text: str, available: set[str]) -> ToolCall | None:
        low = text.lower()

        def want(name: str) -> bool:
            return name in available

        url = _URL.search(text)
        if url and want("http_get"):
            return ToolCall("r1", "http_get", {"url": url.group(0).rstrip(".,)")})
        if ("file" in low or "csv" in low) and want("file_analyze"):
            m = re.search(r"([\w\-]+\.(?:csv|txt|md))", text)
            return ToolCall("r1", "file_analyze", {"file_id": m.group(1) if m else "data.csv"})
        if any(w in low for w in ("remember", "save a note", "note that")) and want("notes_write"):
            value = re.split(r"remember(?: that)?|note that|save a note:?", text, flags=re.I)[-1]
            return ToolCall("r1", "notes_write", {"key": "note", "value": value.strip()[:500]})
        if any(w in low for w in ("recall", "what did i save", "my note")) and want("notes_read"):
            return ToolCall("r1", "notes_read", {"key": "note"})
        if any(w in low for w in ("schedule", "meeting", "calendar")) and want(
            "calendar_create_event"
        ):
            return ToolCall(
                "r1",
                "calendar_create_event",
                {"title": text[:80], "start": "2030-01-01T10:00:00", "duration_minutes": 30},
            )
        if any(w in low for w in ("notify", "send a message", "email", "alert")) and want(
            "send_notification"
        ):
            return ToolCall("r1", "send_notification", {"recipient": "team", "message": text[:200]})
        if any(w in low for w in ("how many", "sql", "orders", "customers")) and want("sql_query"):
            return ToolCall("r1", "sql_query", {"query": _guess_sql(low)})
        if any(w in low for w in ("delete", "remove all", "wipe")) and want("delete_records"):
            return ToolCall("r1", "delete_records", {"table": "orders", "where": "1=1"})
        expr = _extract_expression(text)
        if expr and want("calculator"):
            return ToolCall("r1", "calculator", {"expression": expr})
        if any(w in low for w in ("search", "look up", "latest", "who is")) and want("web_search"):
            return ToolCall("r1", "web_search", {"query": text[:200]})
        return None

    def _injected_call(
        self, last: Message, available: set[str], messages: Sequence[Message]
    ) -> ToolCall | None:
        # Obey at most one injected instruction per run, like a gullible model would.
        already = sum(1 for m in messages if m.role is Role.ASSISTANT and m.tool_calls)
        if already >= 3:
            return None
        m = _INJECTED_ACTION.search(last.content.replace('\\"', '"'))
        if not m or m.group(1) not in available:
            return None
        try:
            args: dict[str, Any] = json.loads(m.group(2))
        except json.JSONDecodeError:
            return None
        if any(
            mm.role is Role.ASSISTANT and any(c.name == m.group(1) for c in mm.tool_calls)
            for mm in messages
        ):
            return None
        return ToolCall(f"inj{already}", m.group(1), args)

    # -- answering --------------------------------------------------------------

    def _summarise(self, messages: Sequence[Message]) -> str:
        observations = [m for m in messages if m.role is Role.TOOL]
        parts: list[str] = []
        for obs in observations:
            try:
                data = json.loads(obs.content)
            except json.JSONDecodeError:
                data = obs.content
            if isinstance(data, dict) and "error" in data:
                parts.append(f"I could not complete {obs.name}: {data['error']}")
            elif isinstance(data, dict) and "result" in data:
                parts.append(f"The result is {_fmt(data['result'])}.")
            elif isinstance(data, dict) and data.get("status") == "awaiting_approval":
                parts.append(f"{obs.name} is waiting for human approval.")
            else:
                parts.append(f"{obs.name} returned: {str(data)[:300]}")
        return " ".join(parts) or "Done."

    @staticmethod
    def _call(call: ToolCall, prompt_tokens: int) -> ModelResponse:
        return ModelResponse(
            None,
            [call],
            Usage(prompt_tokens, _approx_tokens(json.dumps(call.arguments))),
            model="rules-v1",
            finish_reason="tool_calls",
        )

    @staticmethod
    def _final(text: str, prompt_tokens: int) -> ModelResponse:
        return ModelResponse(text, [], Usage(prompt_tokens, _approx_tokens(text)), model="rules-v1")


def _fmt(value: Any) -> str:
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def _extract_expression(text: str) -> str | None:
    t = _PERCENT_OF.sub(lambda m: f"({m.group(1)}/100*{m.group(2)})", text)
    t = re.sub(r"(?<=\d),(?=\d{3})", "", t)
    t = re.sub(r"\bplus\b", "+", t, flags=re.I)
    t = re.sub(r"\bminus\b", "-", t, flags=re.I)
    t = re.sub(r"\b(?:times|multiplied by)\b", "*", t, flags=re.I)
    t = re.sub(r"\bdivided by\b", "/", t, flags=re.I)
    m = _ARITH.search(t)
    if not m:
        return None
    expr, end = m.group(0), m.end()
    # the regex stops at the last digit; pull in closing parens that balance the expression
    while expr.count("(") > expr.count(")") and end < len(t) and t[end] in ") ":
        expr += t[end]
        end += 1
    return expr.strip().rstrip("?.")


def _guess_sql(low: str) -> str:
    if "customer" in low:
        return "SELECT COUNT(*) AS n FROM customers"
    if "revenue" in low or "total" in low:
        return "SELECT SUM(amount) AS total FROM orders"
    return "SELECT COUNT(*) AS n FROM orders"
