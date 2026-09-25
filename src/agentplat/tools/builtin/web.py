"""Tools that read untrusted external content. Every result is marked tainted."""

from __future__ import annotations

import html
import json
import re
from pathlib import Path
from typing import ClassVar
from urllib.parse import urljoin

import httpx
from pydantic import Field

from agentplat.security.ssrf import Resolver, SSRFError, system_resolver, validate_url
from agentplat.tools.base import RiskLevel, Tool, ToolArgs, ToolContext, ToolError, ToolResult

MAX_TEXT_CHARS = 8000
_SCRIPT = re.compile(r"<(script|style)[^>]*>.*?</\1>", re.S | re.I)
_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")


def html_to_text(raw: str) -> str:
    text = _SCRIPT.sub(" ", raw)
    text = _TAG.sub(" ", text)
    return _WS.sub(" ", html.unescape(text)).strip()


class HttpGetArgs(ToolArgs):
    url: str = Field(max_length=2000, description="Absolute http(s) URL to fetch")


class HttpGet(Tool[HttpGetArgs]):
    name: ClassVar[str] = "http_get"
    description: ClassVar[str] = "Fetch a public web page and return its text content."
    args_model = HttpGetArgs
    risk_level = RiskLevel.READ
    required_permissions = frozenset({"web:read"})
    timeout_s = 15.0
    max_retries = 1

    def __init__(
        self,
        *,
        resolver: Resolver = system_resolver,
        transport: httpx.AsyncBaseTransport | None = None,
        max_bytes: int = 500_000,
        max_redirects: int = 3,
        allowed_ports: frozenset[int] = frozenset({80, 443}),
    ) -> None:
        self.resolver = resolver
        self.transport = transport
        self.max_bytes = max_bytes
        self.max_redirects = max_redirects
        self.allowed_ports = allowed_ports

    async def run(self, args: HttpGetArgs, ctx: ToolContext) -> ToolResult:
        url = args.url
        async with httpx.AsyncClient(
            transport=self.transport, follow_redirects=False, timeout=10.0
        ) as client:
            for _hop in range(self.max_redirects + 1):
                try:
                    target = await validate_url(
                        url, self.resolver, allowed_ports=self.allowed_ports
                    )
                except SSRFError as exc:
                    raise ToolError(f"blocked by SSRF policy: {exc}") from exc
                host_header = (
                    target.host if target.port in (80, 443) else f"{target.host}:{target.port}"
                )
                try:
                    async with client.stream(
                        "GET",
                        target.connect_url,  # connect to the IP we validated (no re-resolution)
                        headers={"Host": host_header, "User-Agent": "agentplat/0.1"},
                        extensions={"sni_hostname": target.host},
                    ) as resp:
                        if resp.is_redirect:
                            location = resp.headers.get("location")
                            if not location:
                                raise ToolError("redirect without location")
                            url = urljoin(url, location)
                            continue
                        body, truncated = await self._read_capped(resp)
                        status, ctype = resp.status_code, resp.headers.get("content-type", "")
                except httpx.TimeoutException as exc:
                    raise ToolError("request timed out", transient=True) from exc
                except httpx.TransportError as exc:
                    raise ToolError(f"network error: {exc}", transient=True) from exc
                if status >= 500:
                    raise ToolError(f"upstream HTTP {status}", transient=True)
                text = body.decode("utf-8", errors="replace")
                if "html" in ctype:
                    text = html_to_text(text)
                return ToolResult(
                    {
                        "url": url,
                        "status": status,
                        "content_type": ctype,
                        "text": text[:MAX_TEXT_CHARS],
                        "truncated": truncated or len(text) > MAX_TEXT_CHARS,
                    },
                    tainted=True,
                    source=url,
                )
        raise ToolError(f"too many redirects (> {self.max_redirects})")

    async def _read_capped(self, resp: httpx.Response) -> tuple[bytes, bool]:
        chunks: list[bytes] = []
        size = 0
        async for chunk in resp.aiter_bytes():
            chunks.append(chunk)
            size += len(chunk)
            if size >= self.max_bytes:
                return b"".join(chunks)[: self.max_bytes], True
        return b"".join(chunks), False


class WebSearchArgs(ToolArgs):
    query: str = Field(min_length=1, max_length=300)
    max_results: int = Field(default=3, ge=1, le=10)


class WebSearch(Tool[WebSearchArgs]):
    """Deterministic, offline search over a fixture index (swap for a real API later)."""

    name: ClassVar[str] = "web_search"
    description: ClassVar[str] = "Search the web; returns titles, URLs and snippets."
    args_model = WebSearchArgs
    required_permissions = frozenset({"web:read"})

    def __init__(self, index_file: Path) -> None:
        self.index_file = index_file

    async def run(self, args: WebSearchArgs, ctx: ToolContext) -> ToolResult:
        index = json.loads(self.index_file.read_text()) if self.index_file.exists() else []
        words = set(re.findall(r"\w+", args.query.lower()))
        scored = sorted(
            ((len(words & set(doc.get("keywords", []))), doc) for doc in index),
            key=lambda x: -x[0],
        )
        results = [
            {"title": d["title"], "url": d["url"], "snippet": d["snippet"]}
            for score, d in scored
            if score > 0
        ][: args.max_results]
        return ToolResult(
            {"query": args.query, "results": results}, tainted=True, source="web_search"
        )
