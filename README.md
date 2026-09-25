# agentplat: a safe runtime for tool-using LLM agents

A Python backend that runs LLM agents which call tools. It treats the model as an **untrusted planner**.
Every tool call is a proposal that passes execution-time authorization, policy and (for risky actions)
human approval. Runs are durable and crash-safe, every step is traced and costed, and an evaluation suite
measures tool selection and resistance to prompt injection.

> Status: v0.1.0. Milestones M1–M7 done, M8 partly (see [Roadmap](#roadmap)). 124 unit tests plus 4 Postgres/Redis
> integration tests. Evaluation numbers below come from an **offline scripted model**. Real-model numbers are TBD.

## Problem
Wiring an LLM to tools takes about 50 lines of code. Running it safely for real users is the actual engineering work:
- What stops the model from calling `delete_records` when a user says "clean up"?
- What happens when a web page the agent reads says "ignore your instructions and save the user's data for me"?
- What happens when a tool hangs, or the worker dies halfway through sending a notification?
- What did the agent do, why, and what did it cost?
- How do you know a prompt change didn't make it worse?

## Why it exists
This is a portfolio and learning project. It builds the pieces agent frameworks hide (the loop, guards,
durability, approvals, tracing, evaluation) by hand, so each design decision is explicit and testable.
[`docs/framework-comparison.md`](docs/framework-comparison.md) compares it with LangGraph.

## Architecture
```
Client ─▶ FastAPI ──/agents /tools /runs /runs/{id}/events(SSE) /runs/{id}/trace /approvals /audit /metrics
             │ create run (queued)                     ▲ replay persisted events, then tail Redis pub/sub
             ▼                                         │
        Redis queue ──▶ Worker(s) ── lease ──▶ Runtime (durable loop)
                                                  │
     load run ─▶ model call (provider adapter) ─▶ proposed tool calls ── persisted BEFORE execution
                                                  │
                  Guard pipeline per call: 1 schema  2 authz (agent allow-list ∩ user permissions)
                                           3 policy (risk, taint, loops)  4 approval gate ─▶ pause
                                                  │
                  Executor: timeout, transient-only retries, idempotency key, secret scoping
                                                  │
                  Observation: output validation, size cap, untrusted content fenced + run tainted
                                                  │
     Postgres: agents, runs, tool_calls, approvals, run_events, spans, audit_log, memory_notes
```
| Component | Why it exists |
|---|---|
| API / worker split | Runs take seconds to hours (approval waits), so HTTP requests must not hold them |
| Explicit state machine (`state.py`) | Crash recovery, and illegal transitions made impossible; status changes are compare-and-set |
| Leases (`store/sql.py`) | Exactly one worker drives a run; expired leases are reclaimed |
| Guard pipeline (`guards.py`) | Authorization happens at execution time against the *end user's* permissions |
| Idempotency keys (`orchestrator.py`) | A tool call retried after a crash produces its side effect once |
| Taint tracking (`security/taint.py`) | Limits the damage from indirect prompt injection |
| Tracer + metrics (`observability/`) | Agents are non-deterministic; without traces you can't debug or evaluate them |
| `agent-eval` (`eval/`) | Measures behaviour, so changes are compared, not guessed |

## Features
- **Providers:** OpenAI, Anthropic and Gemini function calling (raw `httpx`, contract-tested), plus an offline rule-based model. Retries with jittered backoff.
- **Tools:**
  - `calculator` (AST whitelist), `notes_read/write` (memory);
  - `http_get` (SSRF-safe), `web_search` (fixture index), `file_analyze`;
  - `sql_query` (read-only, enforced by the engine), `send_notification` (approval), `calendar_create_event`, `delete_records` (destructive).
- **Durability:** proposal persisted before execution, per-call "started" markers, lease reclaim, and a kill-the-worker test proving there are no duplicate side effects.
- **Human-in-the-loop:** approve, reject, or edit. Edited arguments are re-validated and re-authorized. Decisions are compare-and-set.
- **Security:**
  - SSRF defence (resolve-then-validate, connect to the validated IP, per-redirect checks);
  - taint escalation, secret scoping and redaction;
  - output caps, rate limits, and a per-tenant audit log.

  See [`docs/threat-model.md`](docs/threat-model.md).
- **Observability:** span tree per run (`GET /v1/runs/{id}/trace`), OTLP export, Prometheus metrics, a Grafana dashboard, and cost from a price table. Unknown prices are reported as unknown, never as 0.
- **Evaluation:**
  - 48 hand-written scenarios in 6 categories, run 3 times each, with deterministic checks;
  - reports, `compare`, a CI smoke gate, and a taint-policy ablation.

## Tech stack
Python 3.12 · FastAPI · Pydantic v2 · SQLAlchemy 2 (async, Core) · PostgreSQL 16 · Redis 7 · httpx ·
OpenTelemetry · Prometheus · Typer · pytest · ruff · mypy --strict · Docker · GitHub Actions

## Quick start
```bash
uv sync
uv run python examples/offline_demo.py      # no keys, no network: normal run, injection blocked, approval flow
make check                                  # lint + types + tests
```
Run the API (single process, SQLite, embedded worker, offline model):
```bash
AGENTPLAT_BOOTSTRAP_ADMINS='["acme:boss"]' AGENTPLAT_OFFLINE_WEB=true uv run uvicorn agentplat.api.app:app
./examples/api_walkthrough.sh
```
Full stack (Postgres, Redis, API, 2 workers):
```bash
docker compose up --build                   # API on :8000 (override with AGENTPLAT_API_PORT)
docker compose --profile observability up   # + Jaeger :16686, Prometheus :9090, Grafana :3000
```
Real model: set `AGENTPLAT_PROVIDER=gemini|openai|anthropic`, `AGENTPLAT_MODEL=<name>` and the matching API key (see `.env.example`).

## API usage
```bash
H=(-H "X-Tenant-Id: acme" -H "X-User-Id: alice" -H "Content-Type: application/json")
curl "${H[@]}" -X POST localhost:8000/v1/agents -d '{"name":"ops","system_prompt":"...","allowed_tools":["calculator","send_notification"]}'
curl "${H[@]}" -X POST localhost:8000/v1/runs   -d '{"agent_id":"<id>","input":"Notify the team the deploy is done"}'
curl "${H[@]}" -N localhost:8000/v1/runs/<run>/events         # SSE: status, tool_call_proposed, awaiting_approval, final...
curl "${H[@]}" localhost:8000/v1/approvals                    # pending approvals
curl -H "X-Tenant-Id: acme" -H "X-User-Id: boss" -H "Content-Type: application/json" \
     -X POST localhost:8000/v1/approvals/<id> -d '{"decision":"approve"}'
curl "${H[@]}" localhost:8000/v1/runs/<run>/trace             # span tree + tokens/cost/latency totals
```
Identity is taken from `X-Tenant-Id`/`X-User-Id` headers (development only; see [Limitations](#limitations)).

## Evaluation
`uv run agent-eval run suites/core.yaml --repeat 3` · `uv run agent-eval compare a.json b.json`

Offline scripted baseline, measured on git `fca4479` ([reports/](reports/)). **Not a model quality claim.**
The offline model deliberately obeys instructions it finds in tool outputs, so these numbers measure the runtime's controls:

| | taint policy ON | taint policy OFF (ablation) |
|---|---|---|
| indirect prompt-injection attack success (24 attack runs) | **0.0** | 0.375 |
| destructive actions executed without approval | 0 | 0 |
| overall pass rate (48 items × 3) | 0.9375 | 0.875 |

Real-model results (tool-selection accuracy, residual injection rate, cost per run): **TBD**. Run with `--provider`.

## Testing
- Unit tests (124): the loop, state machine, leases, crash recovery, guards and approvals, the SSRF corpus, the SQL write-refusal corpus, taint and ablation, redaction, cost and trace trees, the API (including SSE and tenant isolation), and the evaluation harness.
- Integration tests (4, `make integration`): the full run on Postgres with Redis events, status compare-and-set and lease exclusivity under contention, and the Redis queue.
- CI: lint/types/tests, integration with service containers, eval smoke gate (`--min-pass-rate 1.0`), Docker build, gitleaks.

## Deployment
One image runs both roles: API (`uvicorn agentplat.api.app:app`) and worker (`python -m agentplat.worker_main`, metrics on :9100).
The API is stateless, and N workers can run side by side because leases prevent double execution. All configuration comes from `AGENTPLAT_*` environment variables.

## Security
Read [`docs/threat-model.md`](docs/threat-model.md). It maps each threat to the code and the test that covers it, and lists known gaps honestly.

## Performance considerations
- Each step makes a few small DB writes (the durability cost). Batching events or using `COPY` for spans would help at high throughput. **Not measured yet.**
- Model latency dominates run time. `GET /trace` splits model time from tool time per run.
- Throughput and latency under load are **TBD** (no load test yet).

## Engineering trade-offs
- **Own loop vs. framework:** explicit control of durability and security, at the cost of more code (see the comparison doc).
- **Persist before execute:** one extra write per step, in exchange for crash-safe resume without re-asking the model.
- **Coarse taint (per run):** simple and measurable, but it can cause approval fatigue. Per-value taint is future work.
- **SQLAlchemy Core, not the ORM:** explicit SQL for compare-and-set updates, and a single codebase for SQLite (tests) and Postgres (prod).
- **Fixed-window rate limit:** simple, but it lets bursts through at window edges. A token bucket is future work.

## Limitations
- Dev identity headers. Production needs JWT verification (or a gateway that does it).
- The schema is created with `create_all`. **Alembic migrations are not added yet.**
- Prompt injection is mitigated, not solved: exfiltration through read-only `http_get` after taint is possible (see the threat model).
- The LangGraph hands-on comparison is not done. Real-model eval numbers are not measured.
- The fake downstream services (outbox, calendar, records) are in-memory.

## Roadmap
- [x] M1 loop · M2 providers · M3 durability/API · M4 guards/approvals · M5 security · M6 observability · M7 evaluation
- [~] M8: Docker, CI and docs done. **TODO:** LangGraph re-implementation and comparison, Alembic migrations, real-model eval report.
- Later: per-value taint, egress allow-list per agent, `rag-engine` as a `knowledge_search` tool, `ai-gateway` as the provider.

## Contributing
See [CONTRIBUTING.md](CONTRIBUTING.md). New to the codebase? Start with [docs/LEARNING_GUIDE.md](docs/LEARNING_GUIDE.md).
