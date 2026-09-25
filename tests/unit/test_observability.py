"""M6: traces mirror the run, costs are summed correctly, metrics are exposed."""

from collections.abc import Callable
from typing import Any

import httpx
import pytest

from agentplat.messages import ModelResponse, ToolCall, Usage
from agentplat.observability.metrics import REGISTRY
from agentplat.observability.pricing import Price, PriceTable
from agentplat.observability.tracing import build_trace_tree
from agentplat.providers.fake import ScriptedProvider
from agentplat.state import RunStatus
from agentplat.store.sql import SqlStore
from tests.conftest import MakeRuntime

PRICES = PriceTable(
    {
        "gpt-x": Price(2.0, 8.0),  # fixed test prices, USD per 1M tokens
        "gpt-x-mini": Price(0.5, 1.0),
        "mystery": Price(None, None),
    }
)


def resp(
    tokens_in: int, tokens_out: int, model: str = "gpt-x", tool: str | None = None
) -> ModelResponse:
    calls = [ToolCall("c", tool, {"expression": "1+1"})] if tool else []
    return ModelResponse(None if tool else "done", calls, Usage(tokens_in, tokens_out), model=model)


def test_price_lookup_exact_then_longest_prefix() -> None:
    assert PRICES.lookup("gpt-x-mini-2026") == Price(0.5, 1.0)
    assert PRICES.lookup("gpt-x-2026") == Price(2.0, 8.0)
    assert PRICES.lookup("other") is None


def test_cost_formula_and_unknown_prices() -> None:
    assert PRICES.cost("gpt-x", Usage(1_000_000, 500_000)) == pytest.approx(6.0)
    assert PRICES.cost("mystery", Usage(10, 10)) is None
    assert PRICES.cost("unlisted", Usage(10, 10)) is None


def test_shipped_price_file_parses() -> None:
    table = PriceTable.load("config/models.yaml")
    assert table.cost("rules-v1", Usage(100, 100)) == 0.0


async def test_run_cost_and_trace_tree(
    make_runtime: MakeRuntime, make_agent: Callable[..., Any], store: SqlStore
) -> None:
    agent = await make_agent()
    provider = ScriptedProvider(
        [resp(1000, 100, tool="calculator"), resp(2000, 50, model="gpt-x-mini")]
    )
    rt = make_runtime(provider, prices=PRICES)
    run = await rt.execute((await rt.start_run(agent, "u", "x")).id)
    expected = (1000 * 2 + 100 * 8) / 1e6 + (2000 * 0.5 + 50 * 1) / 1e6
    assert run.est_cost_usd == pytest.approx(expected)

    tree = build_trace_tree(await store.list_spans(run.id))
    (root,) = tree
    assert root["name"] == "run.execute" and root["attributes"]["status"] == "succeeded"
    kinds = [ch["kind"] for ch in root["children"]]
    assert kinds == ["model_call", "tool_call", "model_call"]
    model_span = root["children"][0]
    assert model_span["attributes"]["prompt_tokens"] == 1000
    assert model_span["attributes"]["cost_usd"] == pytest.approx(0.0028)
    assert root["children"][1]["attributes"]["decision"] == "allowed"


async def test_cost_budget_stops_run(
    make_runtime: MakeRuntime, make_agent: Callable[..., Any]
) -> None:
    agent = await make_agent(max_cost_usd=0.001)
    provider = ScriptedProvider([resp(1000, 100, tool="calculator")] * 5)
    rt = make_runtime(provider, prices=PRICES)
    run = await rt.execute((await rt.start_run(agent, "u", "x")).id)
    assert run.status is RunStatus.BUDGET_EXCEEDED
    assert run.step_count == 1


async def test_failed_model_call_is_an_error_span(
    make_runtime: MakeRuntime, make_agent: Callable[..., Any], store: SqlStore
) -> None:
    agent = await make_agent()
    rt = make_runtime(ScriptedProvider([RuntimeError("provider down")]))
    run = await rt.execute((await rt.start_run(agent, "u", "x")).id)
    spans = await store.list_spans(run.id)
    model = next(s for s in spans if s["kind"] == "model_call")
    assert model["status"] == "error" and "provider down" in model["attributes"]["error"]


def test_metrics_registered() -> None:
    names = {m.name for m in REGISTRY.collect()}
    for expected in [
        "agent_runs",
        "agent_tool_calls",
        "agent_tokens",
        "agent_cost_usd",
        "agent_model_latency_seconds",
        "agent_approvals_pending",
    ]:
        assert expected in names


async def test_trace_and_metrics_endpoints(tmp_path: Any) -> None:
    from agentplat.api.app import create_app
    from agentplat.config import Settings
    from agentplat.container import build_container

    c = build_container(
        Settings(
            database_url=f"sqlite+aiosqlite:///{tmp_path / 't.db'}",
            embedded_worker=False,
            _env_file=None,
        )
    )
    app = create_app(c)
    h = {"X-Tenant-Id": "acme", "X-User-Id": "u"}
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as cl,
    ):
        agent = (
            await cl.post(
                "/v1/agents",
                json={"name": "a", "system_prompt": "s", "allowed_tools": ["calculator"]},
                headers=h,
            )
        ).json()
        run = (
            await cl.post(
                "/v1/runs",
                json={"agent_id": agent["id"], "input": "2*21", "mode": "sync"},
                headers=h,
            )
        ).json()
        trace = (await cl.get(f"/v1/runs/{run['id']}/trace", headers=h)).json()
        assert trace["totals"]["model_calls"] == 2 and trace["totals"]["tool_calls"] == 1
        text = (await cl.get("/metrics")).text
        assert 'agent_tool_calls_total{decision="allowed",tool="calculator"}' in text
    await c.close()
