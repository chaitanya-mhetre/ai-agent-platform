from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from agentplat.api.app import create_app, stream_events
from agentplat.config import Settings
from agentplat.container import Container, build_container

H = {"X-Tenant-Id": "acme", "X-User-Id": "alice"}


@pytest.fixture
async def container(tmp_path: Path) -> AsyncIterator[Container]:
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'api.db'}",
        embedded_worker=False,
        provider="fake",
        _env_file=None,
    )
    c = build_container(settings)
    yield c
    await c.close()


@pytest.fixture
async def client(container: Container) -> AsyncIterator[httpx.AsyncClient]:
    app: FastAPI = create_app(container)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            yield c


async def make_agent(client: httpx.AsyncClient, **kw: Any) -> str:
    body = {"name": "calc", "system_prompt": "use tools", "allowed_tools": ["calculator"], **kw}
    r = await client.post("/v1/agents", json=body, headers=H)
    assert r.status_code == 201, r.text
    return str(r.json()["id"])


async def test_health_and_ready(client: httpx.AsyncClient) -> None:
    assert (await client.get("/health")).json() == {"status": "ok"}
    assert (await client.get("/ready")).json() == {"status": "ready"}


async def test_identity_required(client: httpx.AsyncClient) -> None:
    assert (await client.get("/v1/agents")).status_code == 401


async def test_unknown_tool_rejected(client: httpx.AsyncClient) -> None:
    r = await client.post(
        "/v1/agents",
        json={"name": "x", "system_prompt": "x", "allowed_tools": ["rm_rf"]},
        headers=H,
    )
    assert r.status_code == 422


async def test_tools_listing(client: httpx.AsyncClient) -> None:
    tools = {t["name"]: t for t in (await client.get("/v1/tools")).json()}
    assert tools["notes_write"]["risk_level"] == "write"
    assert tools["notes_write"]["required_permissions"] == ["notes:write"]


async def test_sync_run_with_fake_model(client: httpx.AsyncClient) -> None:
    agent_id = await make_agent(client)
    r = await client.post(
        "/v1/runs",
        json={"agent_id": agent_id, "input": "What is 17% of 2,340 plus 12?", "mode": "sync"},
        headers=H,
    )
    assert r.status_code == 201
    body = r.json()
    assert body["status"] == "succeeded"
    assert "409.8" in body["final_output"]

    detail = (await client.get(f"/v1/runs/{body['id']}", headers=H)).json()
    assert detail["tool_calls"][0]["tool"] == "calculator"
    assert detail["tool_calls"][0]["status"] == "succeeded"


async def test_background_run_processed_by_worker_and_events_replay(
    client: httpx.AsyncClient, container: Container
) -> None:
    agent_id = await make_agent(client)
    r = await client.post(
        "/v1/runs", json={"agent_id": agent_id, "input": "compute 6 * 7"}, headers=H
    )
    run_id = r.json()["id"]
    assert r.json()["status"] == "queued"

    assert await container.worker().process_one(timeout_s=1) == run_id
    run = (await client.get(f"/v1/runs/{run_id}", headers=H)).json()
    assert run["status"] == "succeeded" and "42" in run["final_output"]

    events = [e async for e in stream_events(container, run_id)]
    types = [e["event"] for e in events]
    assert types[0] == "status" and "tool_call_proposed" in types and "final" in types
    assert [int(e["id"]) for e in events] == sorted(int(e["id"]) for e in events)

    sse = await client.get(f"/v1/runs/{run_id}/events", headers=H)
    assert sse.status_code == 200 and "event: final" in sse.text


async def test_runs_are_tenant_isolated(client: httpx.AsyncClient) -> None:
    agent_id = await make_agent(client)
    run_id = (
        await client.post(
            "/v1/runs", json={"agent_id": agent_id, "input": "1+1", "mode": "sync"}, headers=H
        )
    ).json()["id"]
    other = {"X-Tenant-Id": "evil", "X-User-Id": "mallory"}
    assert (await client.get(f"/v1/runs/{run_id}", headers=other)).status_code == 404
    r = await client.post("/v1/runs", json={"agent_id": agent_id, "input": "x"}, headers=other)
    assert r.status_code == 404


async def test_cancel_terminal_run_conflicts(client: httpx.AsyncClient) -> None:
    agent_id = await make_agent(client)
    run_id = (
        await client.post(
            "/v1/runs", json={"agent_id": agent_id, "input": "hi", "mode": "sync"}, headers=H
        )
    ).json()["id"]
    assert (await client.post(f"/v1/runs/{run_id}/cancel", headers=H)).status_code == 409
