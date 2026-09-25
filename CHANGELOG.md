# Changelog

## [0.1.0] - 2026-09-25
### Added
- M1: provider-neutral messages, tool registry, agent loop, scripted fake provider.
- M2: OpenAI / Anthropic / Gemini adapters (raw httpx), retry with jittered backoff, offline rule-based model.
- M3: durable runs (Postgres/SQLite), state machine, leases, crash-safe resume with idempotency keys,
  Redis queue + pub/sub, FastAPI API with SSE replay and live tail.
- M4: guard pipeline (schema → authz → policy → approval), approvals API (approve/reject/edit), audit log.
- M5: SSRF-safe `http_get`, taint tracking and escalation, read-only SQL, file sandboxing, secret scoping,
  redaction, observation caps, rate limits, threat model.
- M6: span tree per run, OpenTelemetry export, cost accounting from a price table, Prometheus metrics,
  Grafana dashboard.
- M7: `agent-eval` with a 48-item suite, deterministic scoring, reports, compare, taint-policy ablation.
- M8: Docker image, CI (lint/type/test, integration, eval smoke gate, image build, secret scan), docs.
