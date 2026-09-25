# Learning guide: how agentplat works

This guide is for self-study. Read it with the code open. Everything an interviewer might ask about this
project should be answerable from here. Budget about 6–8 hours for the first pass. A good exercise is to
re-draw the architecture from memory afterwards.

---

## 1. The big picture in one paragraph
A user sends a request, and a **run** is created in the database with status `queued`. A **worker** takes a
**lease** on the run, so only it can drive the run, and starts the **loop**:
1. It asks the model what to do. The model either answers or proposes **tool calls**.
2. Proposals are **persisted first**, then each one goes through the **guard pipeline**: schema, then authorization, then policy, then approval.
3. Allowed calls run in the **executor**, which applies a timeout, retries, and passes an idempotency key.
4. The result becomes an **observation** message: validated, capped, and fenced if it came from an untrusted source. Then the loop repeats.
5. Risky calls pause the run (`awaiting_approval`) until a human decides through the API. Then the run is re-queued.

Every step emits **events** (for SSE), **spans** (for traces), **metrics** and **audit** entries. The
**evaluation suite** replays scripted scenarios and scores what the agent did.

## 2. File tour: read in this order
| # | File | What to look for |
|---|---|---|
| 1 | `src/agentplat/messages.py` | Provider-neutral `Message`, `ToolCall`, `ModelResponse`. Everything else speaks this format |
| 2 | `providers/base.py`, `providers/fake.py` | The `ModelProvider` Protocol (structural typing); `ScriptedProvider` makes the loop testable |
| 3 | `tools/base.py` | `Tool[A]`: declarative metadata (`risk_level`, `required_permissions`, `requires_approval`, `timeout_s`, `max_retries`). `schema()` builds the JSON Schema from Pydantic with `additionalProperties: false` |
| 4 | `tools/builtin/calculator.py` | Why `eval()` is dangerous, and how an AST whitelist fixes it |
| 5 | `state.py` | `RunStatus` and the `TRANSITIONS` table: "rules as data" |
| 6 | `store/schema.py`, `store/sql.py` | Tables; `transition()` and `acquire_lease()` are single compare-and-set `UPDATE ... WHERE` statements. Read `reclaimable_runs()` |
| 7 | `orchestrator.py` | **The core.** `execute()` → `_drive()` loop → `_process_calls()` → `_handle_call()` → `_run_tool()`. Find the comment "Persist the proposal BEFORE executing anything" |
| 8 | `runtime/executor.py` | Timeout with `asyncio.timeout`; retry only if `ToolError.transient`; a buggy tool can't crash the run |
| 9 | `guards.py` | `GuardPipeline.decide()`: the 4 stages; how an edited approval is re-validated; `_identical_calls` loop detection |
| 10 | `approvals.py` | Deciding is compare-and-set; the run is re-queued only when no approvals are pending |
| 11 | `security/ssrf.py`, `tools/builtin/web.py` | `validate_url()` and `HttpGet.run()`: connect to `target.connect_url` (the IP), send `Host` + SNI, re-validate every redirect |
| 12 | `security/taint.py` + `orchestrator._on_untrusted` + `guards._needs_approval` | How untrusted content escalates later actions |
| 13 | `tools/builtin/data.py` | `SqlQuery`: read-only URI + `set_authorizer` + progress handler. `FileAnalyze`: path containment |
| 14 | `security/redaction.py` | Where redaction is applied: observations, events, audit, spans |
| 15 | `observability/tracing.py`, `pricing.py`, `metrics.py` | Span tree; why an unknown price is `None`, not `0` |
| 16 | `runtime/events.py`, `api/app.py::stream_events` | Subscribe *before* replaying; de-duplicate by `seq` |
| 17 | `runtime/worker.py`, `worker_main.py`, `container.py` | Composition root; how the worker reclaims runs |
| 18 | `eval/suite.py`, `eval/runner.py` | Isolated runtime per item; checks; metrics; the ablation flag |
| 19 | `providers/openai.py`, `anthropic.py`, `gemini.py` | Wire-format differences (see §4.1) |
| 20 | `tests/unit/test_runtime.py::test_worker_crash_mid_tool_resumes_without_duplicate_side_effect` | The most important test in the repo. Understand every line |

## 3. Walk one request through the code
`POST /v1/runs {"input": "Delete all orders"}` with user permission `records:delete`:
1. `api/app.py::create_run`: rate limit → `runtime.start_run` (status `queued`, system prompt + `SECURITY_PREAMBLE`) → `queue.enqueue`.
2. The worker calls `Runtime.execute(run_id)` and acquires the lease (`UPDATE runs SET lease_owner=... WHERE lease is free or expired`).
3. `_drive`: `queued → running` (compare-and-set). No pending calls, so it checks budgets and then `_call_model` (span `model.<provider>`, tokens, cost).
4. The model proposes `delete_records`. Its id is rewritten to `call_1_0`, the assistant message is **saved**, and `tool_call_proposed` is emitted.
5. Next iteration: `unanswered_calls` finds `call_1_0` → `_handle_call`:
   - the guard checks the schema: OK;
   - authorization: the tool is on the allow-list and the user has `records:delete`, so OK;
   - policy: `DESTRUCTIVE` is in `always_approve`, so the verdict is `needs_approval`.
6. `_request_approval` inserts an `approvals` row and a tool-call record (`awaiting_approval`), then audits and emits. It raises `Paused`, so the run goes `running → awaiting_approval` and the lease is released.
7. A human with `approvals:decide` calls `POST /v1/approvals/{id}` (approve). The decision is compare-and-set. With no approvals left pending, the run goes `awaiting_approval → queued` and is enqueued.
8. A worker resumes. The guard sees the approval as `approved`, so the verdict is `allowed`. `_run_tool` marks the call `started`, the executor runs it with the idempotency key, and the result is stored.
9. The observation is appended and the model is called again. It gives a final answer, the run becomes `succeeded`, metrics are updated, and the lease is released.

## 4. Concepts, with code pointers

### 4.1 Tool calling (function calling)
The model gets a tool list: name, description and JSON Schema. It returns a structured "call" instead of text.
The runtime executes it and sends the result back as a special message. Every provider does this differently:
- **OpenAI:** `tool_calls[].function.arguments` is a **JSON string** (it can be malformed; see `parse_openai_response`). Results go back as `role: "tool"` with a `tool_call_id`.
- **Anthropic:** `tool_use` content blocks. Results are `tool_result` blocks inside a **user** turn, and several consecutive results must share one turn (`to_anthropic_messages`).
- **Gemini:** `functionCall` / `functionResponse` parts. It only accepts a subset of JSON Schema, so `to_gemini_schema` strips unsupported keys, and older responses have no call ids.

### 4.2 The model as an untrusted planner
The model's output is a *proposal*. The prompt tells the model which tools exist. That's a hint, not security.
The security check is `GuardPipeline.decide()`, which runs **at execution time** with the **end user's** permissions:
`allowed = tool ∈ agent.allowed_tools ∧ tool.required_permissions ⊆ user.permissions ∧ policy(run, tool)`.
Denials go back to the model as observations, so it can adapt, and they are audited.

### 4.3 Permission model
- Permissions are strings (`notes:write`, `records:delete`) held per (tenant, user) in `user_permissions`.
- Each tool declares what it needs, and each agent declares which tools it may use. The effective ability is the **intersection** of the two.
- Admin and reviewer powers are permissions too (`admin`, `approvals:decide`).

### 4.4 Approval flow
- `DESTRUCTIVE` always needs approval, `requires_approval=True` tools always do, and a tainted run needs approval for `WRITE`/`EXTERNAL`/`DESTRUCTIVE`.
- The run's state is persisted, so approval can arrive days later.
- **Edit** gets re-validated by the schema. A reviewer adding a `bcc` field is rejected by `extra="forbid"` (see `test_invalid_edited_args_rejected_by_schema`).
- **Reject** becomes an observation, and the agent continues.

### 4.5 Prompt injection
- **Direct:** the user writes "ignore your rules…". Authorization still applies, so a user can't exceed their own permissions.
- **Indirect:** content the agent *reads* contains instructions. Mitigations here:
  1. fence untrusted output as data (`fence_untrusted`, with closing tags escaped);
  2. a preamble telling the model that fenced content is data;
  3. **taint escalation**: after reading untrusted content, risky actions need a human;
  4. heuristic signals get audited.
- The ablation measures (3): on the offline gullible model, attack success is 0.375 without it and 0.0 with it.
- Honest limit: this doesn't stop a *misleading answer*, or exfiltration through read-only fetches.

### 4.6 SSRF
- Agents fetch URLs chosen by the model, and the model is steerable by attackers. Internal targets include the cloud metadata service (169.254.169.254, which serves IAM credentials), localhost admin panels and the VPC.
- The checks are:
  - an allow-list of schemes and ports, and no `user@host` credentials in the URL;
  - **resolve DNS yourself, and reject if any address is non-public**;
  - **connect to the validated IP**, sending `Host` and SNI for the name. A second DNS lookup can't swap in 127.0.0.1 ("DNS rebinding");
  - **re-validate every redirect**, since a public page can 302 to the metadata service.
- Numeric encodings like `http://2130706433/` are refused.

### 4.7 Durability, leases and idempotency
- A **lease** is a lock with an expiry, so a dead worker's runs become claimable (`reclaimable_runs`).
- **Persist before execute:** after a crash, the new worker rebuilds exactly which calls are unanswered.
- An **idempotency key** is `sha256(run_id | call_id | tool | canonical args)`. Downstream services dedupe on it, so a retry never double-sends. Canonical JSON (`sort_keys`) makes the key independent of argument order.
- The crash test raises a `BaseException` (like SIGKILL) *after* the side effect and *before* the result is recorded.

### 4.8 Tracing and cost
- Spans form the tree `run.execute → model.* | tool.*`, with tokens, cost, decision, attempts and errors as attributes.
- They're stored in Postgres, so the trace endpoint works without extra infrastructure, and mirrored to OTel.
- Cost is `tokens × price per 1M` from `config/models.yaml`. An unknown price is `None`, and it is counted in `agent_unpriced_model_calls_total` rather than reported as $0.

### 4.9 Evaluation
- Agents are non-deterministic, so each item repeats N times and the report shows variance ("flaky items").
- Checks are deterministic: which tools were proposed or executed, which approvals were requested, text in the final answer, side effects in memory.
- An **ablation** turns one control off to prove that control is what causes the effect.
- The offline model's numbers measure the *runtime*, not a model. The report labels them that way.

## 5. Interview questions, with answers

1. **Walk me through the agent loop.** Model call → if it answers, finish; otherwise persist the proposals → for each call: guard (schema → authz → policy → approval) → executor → observation → repeat until a final answer, the step budget or the cost budget.
2. **Why treat the model as untrusted?** Its output can be steered by users and by any content it reads. Anything it proposes must be checked against the real user's permissions before it executes.
3. **Where does authorization happen, and why not in the prompt?** In `GuardPipeline` at execution time. Prompts are suggestions; a model can hallucinate or be tricked into calling unlisted tools.
4. **How do you stop a destructive action?** A `DESTRUCTIVE` risk level always requires human approval, plus the permission check. It's tested by `test_authorization_matrix` and the eval metric "destructive executed without approval = 0".
5. **What is indirect prompt injection, and how do you mitigate it?** Instructions hidden in content the agent reads. Fence it as data, taint the run, escalate risky actions to a human, audit the signals. You can't fully solve it; you limit the blast radius and measure what's left.
6. **How do you know the taint policy works?** An ablation: the same suite with the policy off. Attack success went from 0.375 to 0.0 on the offline model.
7. **Explain SSRF and your defence.** See §4.6. The key points: validate *after* DNS resolution, connect to the validated IP, and re-check every redirect.
8. **What is DNS rebinding?** An attacker's DNS answers with a public IP for the check and a private IP for the actual connection. Connecting to the IP you validated defeats it.
9. **How do you get exactly-once side effects with retries?** Idempotency keys, plus deduplication in the downstream service. Pure exactly-once delivery is impossible, but at-least-once delivery plus idempotency gives exactly-once *effects*.
10. **What happens if a worker dies mid-run?** The lease expires and the reclaimer re-enqueues the run. The new worker finds the unanswered calls from persisted messages, and a call marked `started` is retried with the same key.
11. **Why compare-and-set for status changes?** Two workers or two API calls could race. `UPDATE ... WHERE status = 'queued'` lets exactly one win (see the integration test with 10 concurrent attempts).
12. **Why persist the proposal before executing it?** Otherwise a crash loses the model's decision. The resumed run would ask the model again and might choose a different action, or repeat a side effect.
13. **How does SSE streaming work with background runs?** The worker publishes events to Redis pub/sub and stores them with a sequence number. The endpoint subscribes first, replays stored events, then tails live ones, de-duplicating by `seq` so late subscribers see everything.
14. **Why is `sql_query` safe?** A read-only connection, *plus* an sqlite authorizer that denies every non-read action (so the engine refuses writes, ATTACH and PRAGMA), plus a single-statement rule, a VM step limit and a row cap. It doesn't rely on string matching.
15. **How do you keep secrets out of the model?** They're scoped per tool in `SecretStore` and delivered through `ToolContext`, never through arguments. `Redactor` masks known values and key patterns in observations, events, audit and spans.
16. **How do you handle tool timeouts and failures?** `asyncio.timeout` per tool, and retries with jittered backoff only for transient errors. Validation and permission failures are never retried. A crashing tool becomes an error observation.
17. **How do you detect an agent stuck in a loop?** The step budget, plus blocking identical (tool, args) calls after N repeats. A denial observation tells the model to change approach.
18. **How do you track cost?** Per model call: tokens × price table. Summed on the run, enforced as a budget before each call, and exported as a Prometheus counter. Unknown prices are flagged, not zeroed.
19. **How do you evaluate an agent?** Hand-written scenarios with deterministic checks, repeated runs for variance, per-category metrics (tool precision/recall, attack success, unsafe actions), `compare` for regressions, and a CI smoke gate.
20. **Why hand-write the loop instead of using LangGraph?** To control and demonstrate durability, security and evaluation explicitly. LangGraph gives graphs, checkpointers and `interrupt()`, but authz and taint would still be custom code. See the comparison doc.
21. **What's the difference between LangGraph checkpoints and your approach on crash?** Both resume from saved state. But a node that crashes after a side effect and before its checkpoint re-runs that side effect. This repo pairs persistence with idempotency keys.
22. **How would you scale this?** The API is stateless, and N workers are safe thanks to leases. Postgres is the bottleneck: add indexes (already there on status and time), partition `run_events`/`spans` by time, and batch span writes. The Redis queue could move to Redis Streams for consumer groups and acknowledgements.
23. **Biggest limitations?** Dev identity headers, no Alembic yet, coarse per-run taint (approval fatigue), exfiltration through read-only fetches after taint, and no real-model numbers yet.
24. **How is multi-tenancy enforced?** Every query filters by `tenant_id` from the principal. Another tenant's ids return 404 (so existence doesn't leak), and there's a test for it.
25. **What does `additionalProperties: false` buy you?** Models sometimes invent extra arguments. Rejecting them stops smuggled fields, like the `bcc` example, and turns the mistake into feedback the model can act on.

## 6. Exercises (to make it yours)
1. Add a `translate_text` tool with a `language` enum and a `WRITE`-risk variant. Write the authorization matrix tests first.
2. Implement per-agent **egress allow-lists** for `http_get`, and add an eval item for exfiltration through a query string.
3. Replace `create_all` with Alembic migrations.
4. Add a `knowledge_search` tool that calls the `rag-engine` project.
5. Run the core suite against a real model and write up the results honestly in `reports/`.
