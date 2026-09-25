"""Span tree per run: run.execute -> model_call | tool_call.

Spans are stored in the database (so GET /v1/runs/{id}/trace works with no
extra infrastructure) and mirrored to OpenTelemetry when an exporter is
configured, so the same data shows up in Jaeger/Tempo/Grafana.
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from opentelemetry import trace as otel_trace
from opentelemetry.trace import Span as OtelSpan
from opentelemetry.trace import Status, StatusCode

from agentplat.security.redaction import Redactor
from agentplat.store.sql import SqlStore, new_id, utcnow


@dataclass(slots=True)
class Span:
    id: str
    run_id: str
    parent_id: str | None
    kind: str
    name: str
    attributes: dict[str, Any] = field(default_factory=dict)
    status: str = "ok"
    otel: OtelSpan | None = None

    def set(self, **attrs: Any) -> None:
        self.attributes.update({k: v for k, v in attrs.items() if v is not None})

    def fail(self, error: str) -> None:
        self.status = "error"
        self.attributes["error"] = error


class Tracer:
    def __init__(self, store: SqlStore, redactor: Redactor | None = None) -> None:
        self.store = store
        self.redactor = redactor or Redactor()
        self._otel = otel_trace.get_tracer("agentplat")

    @asynccontextmanager
    async def span(
        self, run_id: str, kind: str, name: str, parent: Span | None = None, **attrs: Any
    ) -> AsyncIterator[Span]:
        ctx = otel_trace.set_span_in_context(parent.otel) if parent and parent.otel else None
        otel_span = self._otel.start_span(name, context=ctx)
        span = Span(
            new_id(), run_id, parent.id if parent else None, kind, name, dict(attrs), otel=otel_span
        )
        started = utcnow()
        t0 = time.perf_counter()
        try:
            yield span
        except BaseException as exc:
            span.fail(f"{type(exc).__name__}: {exc}")
            raise
        finally:
            duration_ms = round((time.perf_counter() - t0) * 1000, 3)
            attributes = self.redactor.deep(span.attributes)
            for k, v in attributes.items():
                if isinstance(v, str | bool | int | float):
                    otel_span.set_attribute(f"agent.{k}", v)
            otel_span.set_attribute("agent.run_id", run_id)
            if span.status == "error":
                otel_span.set_status(Status(StatusCode.ERROR))
            otel_span.end()
            await self.store.insert_span(
                {
                    "id": span.id,
                    "run_id": run_id,
                    "parent_id": span.parent_id,
                    "kind": kind,
                    "name": name,
                    "attributes": attributes,
                    "status": span.status,
                    "started_at": started,
                    "ended_at": started + timedelta(milliseconds=duration_ms),
                    "duration_ms": duration_ms,
                }
            )


def build_trace_tree(spans: list[dict[str, Any]]) -> list[dict[str, Any]]:
    nodes = {
        s["id"]: {
            "id": s["id"],
            "kind": s["kind"],
            "name": s["name"],
            "status": s["status"],
            "duration_ms": s["duration_ms"],
            "started_at": s["started_at"].isoformat() if s["started_at"] else None,
            "attributes": s["attributes"],
            "children": [],
        }
        for s in spans
    }
    roots: list[dict[str, Any]] = []
    for s in spans:
        node = nodes[s["id"]]
        parent = nodes.get(s["parent_id"]) if s["parent_id"] else None
        (parent["children"] if parent else roots).append(node)
    return roots


def configure_otel(endpoint: str | None, service_name: str = "agentplat") -> None:
    """Export spans over OTLP/HTTP when an endpoint is configured."""
    if not endpoint:
        return
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    provider = TracerProvider(resource=Resource.create({"service.name": service_name}))
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint)))
    otel_trace.set_tracer_provider(provider)
