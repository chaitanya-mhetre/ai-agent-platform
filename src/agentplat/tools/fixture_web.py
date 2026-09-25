"""An offline "internet" for evaluation and demos.

Hosts ending in `.test` resolve to a public-looking address and are served from
fixtures/pages/<host>/<path>.html, so web tools behave realistically (including
redirects and injected content) with no network access.
"""

from __future__ import annotations

from pathlib import Path

import httpx

from agentplat.security.ssrf import Resolver

FAKE_PUBLIC_IP = "93.184.215.14"


def fixture_resolver_for(pages_dir: Path) -> Resolver:
    async def resolve(host: str, port: int) -> list[str]:
        if host.endswith(".test"):
            return [FAKE_PUBLIC_IP]
        raise OSError(f"offline mode: cannot resolve {host}")

    return resolve


def fixture_transport(pages_dir: Path) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        host = request.headers.get("host", "").split(":")[0]
        path = request.url.path.strip("/") or "index"
        redirect = pages_dir / host / f"{path}.redirect"
        if redirect.exists():
            return httpx.Response(302, headers={"location": redirect.read_text().strip()})
        page = pages_dir / host / f"{path}.html"
        if not page.exists():
            return httpx.Response(404, text="not found", headers={"content-type": "text/plain"})
        return httpx.Response(200, text=page.read_text(), headers={"content-type": "text/html"})

    return httpx.MockTransport(handler)
