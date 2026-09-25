# Changelog

## [Unreleased]
### Changed
- Schema is managed by Alembic (revision `0001`) instead of `metadata.create_all`. API and worker run
  `upgrade head` on startup under a Postgres advisory lock; `AGENTPLAT_AUTO_CREATE_SCHEMA` is renamed to
  `AGENTPLAT_AUTO_MIGRATE`. Databases created by the old `create_all` are stamped at `0001`. (#1)
### Added
- `agentplat-db` CLI (`upgrade`, `downgrade`, `current`, `revision`) and `make migrate` / `make migration`. (#1)
- Migration tests (round trip, idempotency, drift, legacy adoption, concurrent migrators) and a CI round trip
  on real Postgres. (#1)

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
