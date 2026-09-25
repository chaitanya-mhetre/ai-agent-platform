"""Prometheus metrics. Names follow <namespace>_<thing>_<unit>."""

from __future__ import annotations

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram

REGISTRY = CollectorRegistry()

RUNS = Counter("agent_runs_total", "Runs reaching a status", ["status"], registry=REGISTRY)
STEPS_PER_RUN = Histogram(
    "agent_steps_per_run",
    "Model steps per finished run",
    buckets=(1, 2, 3, 4, 6, 8, 12, 20, 50),
    registry=REGISTRY,
)
TOOL_CALLS = Counter(
    "agent_tool_calls_total", "Tool call decisions", ["tool", "decision"], registry=REGISTRY
)
TOOL_LATENCY = Histogram(
    "agent_tool_latency_seconds", "Tool execution latency", ["tool"], registry=REGISTRY
)
MODEL_LATENCY = Histogram(
    "agent_model_latency_seconds", "Model call latency", ["model"], registry=REGISTRY
)
TOKENS = Counter("agent_tokens_total", "Tokens used", ["model", "type"], registry=REGISTRY)
COST = Counter("agent_cost_usd_total", "Estimated model cost (USD)", ["model"], registry=REGISTRY)
UNPRICED_CALLS = Counter(
    "agent_unpriced_model_calls_total",
    "Model calls with unknown price",
    ["model"],
    registry=REGISTRY,
)
APPROVALS_PENDING = Gauge(
    "agent_approvals_pending", "Approvals waiting for a human", registry=REGISTRY
)
INJECTION_SIGNALS = Counter(
    "agent_injection_signals_total",
    "Tool outputs with injection phrasing",
    ["tool"],
    registry=REGISTRY,
)
