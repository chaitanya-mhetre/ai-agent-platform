from __future__ import annotations

from typing import Any

import httpx

from agentplat.providers.base import ProviderError

TRANSIENT_STATUS = {408, 409, 425, 429, 500, 502, 503, 504, 529}


async def post_json(
    client: httpx.AsyncClient, url: str, *, json: dict[str, Any], headers: dict[str, str]
) -> dict[str, Any]:
    try:
        resp = await client.post(url, json=json, headers=headers)
    except (httpx.TimeoutException, httpx.NetworkError) as exc:
        raise ProviderError(f"network error: {exc}", transient=True) from exc
    if resp.status_code >= 400:
        # Never include request headers in errors: they carry API keys.
        raise ProviderError(
            f"HTTP {resp.status_code}: {resp.text[:300]}",
            transient=resp.status_code in TRANSIENT_STATUS,
            status=resp.status_code,
        )
    data: dict[str, Any] = resp.json()
    return data
