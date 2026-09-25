# Hand-written runtime vs. LangGraph

**Status: hands-on comparison done for one scenario** (issue #3). The "risky tools need a human"
flow is re-implemented on LangGraph in [`comparison/langgraph_flow.py`](../comparison/langgraph_flow.py),
with tests in [`tests/comparison/test_langgraph_flow.py`](../tests/comparison/test_langgraph_flow.py).
Versions: `langgraph` 1.2.12, `langchain-core` 1.6.5 (optional dependency group `comparison`).

Both versions share the same model providers (`ScriptedProvider`), tools (`delete_records`,
`send_notification`, `calendar_create_event`) and fake services, so the comparison isolates orchestration.
Everything here was run offline with the scripted model; no model-quality claims.

## What was built on LangGraph

```
START -> agent --(tool calls and steps < max)--> tools -> agent ... -> END
```
- `agent` node: calls the provider, appends the assistant message.
- `tools` node: for each proposed call, allow-list ∩ user permissions → schema validation → approval gate
  (`interrupt()`) → execute with a timeout and an idempotency key.
- `InMemorySaver` checkpointer, keyed by `thread_id`; resume with `Command(resume={"decision": ...})`.

## Results

| Check (same scenario, same scripted model) | Hand-built | LangGraph |
|---|---|---|
| Destructive call pauses, runs exactly once after approval | ✅ | ✅ |
| Rejection becomes an observation and the run continues | ✅ | ✅ |
| Missing permission → denied without asking a human | ✅ | ✅ (written by hand in the node) |
| Tool not on allow-list → denied | ✅ | ✅ (written by hand in the node) |
| Extra arguments rejected | ✅ | ✅ (reuses `ToolArgs extra="forbid"`) |
| Same final status, output and side effects (parity test) | ✅ | ✅ |
| Side effect executed *before* the interrupt in the same step | runs once (proposal + record persisted before execution) | **runs twice**; single effect only because of the idempotency key |

### The finding worth remembering
`test_side_effects_before_interrupt_rerun_on_resume`: one model turn proposes a calendar write (no approval
needed) and a delete (approval needed). On LangGraph the calendar tool **executes again** when the graph
resumes, because `interrupt()` resumes by re-running the whole node from the top (documented behaviour).
The fake calendar still ends with one event only because every call carries an idempotency key
(`sha256(thread_id, call_id, name, args)`). Without that key, the side effect would duplicate.

The hand-built runtime avoids the re-run structurally: each call's outcome is persisted in `tool_calls`
before moving on, and a resumed run only processes calls without an observation (`unanswered_calls`).

Fixes on LangGraph: make every side effect idempotent (as here), put side-effecting calls in their own node
after the approval node, or split into one node per call.

## Size (code lines, excluding blank lines, comments and docstrings)

| | Lines | Covers |
|---|---|---|
| `comparison/langgraph_flow.py` | 162 | loop, authz, validation, approval pause/resume, timeout, idempotency key, driver |
| `orchestrator.py` + `guards.py` + `approvals.py` + `runtime/executor.py` + `runtime/worker.py` + `state.py` | 464 + 93 + 49 + 54 + 66 + 44 = 770 | the above **plus** durable run rows and leases, crash-safe resume across processes, retries with backoff, taint escalation, loop detection, budgets (steps/tokens/cost), output validation and caps, audit log, tracing spans, cancellation, edit-and-approve |

The ~4.7× difference is mostly features the LangGraph version doesn't have, not framework savings.
A fair estimate: LangGraph removes the loop, state persistence and pause/resume plumbing (roughly the
`_drive`/`execute`/`Paused` and approval re-queue code, ~150–200 lines). The security and correctness
controls would be the same size either way.

## Control, tracing, testability

- **Permissions and approval:** LangGraph has no authorization model. All checks live inside the `tools`
  node as custom code, same as the hand-built guard pipeline, just less isolated (the hand-built
  `GuardPipeline` is testable without running the loop).
- **Approval semantics:** `interrupt()` is elegant for the happy path. But approval state lives in the
  checkpoint, not a queryable `approvals` table, so "list all pending approvals for tenant X" or an
  approvals API needs an extra store or a scan over threads. Edit-and-approve needs extra resume payload logic.
- **Durability:** the checkpointer saves state per super-step. Cross-process crash recovery needs a
  persistent saver (Postgres/SQLite packages) plus something to *notice* a dead run and re-invoke it; the
  hand-built lease + reclaimer does that. Not tested here (in-memory saver only).
- **Tracing:** the hand-built runtime writes its own span tree and OTel spans. LangGraph integrates with
  LangSmith or LangChain callbacks; the comparison version has no tracing.
- **Testability:** both are easy to test offline with a scripted model. LangGraph state is inspectable via
  `aget_state()` (`snapshot.interrupts`, `snapshot.next`), which made the tests short.

## When I'd choose which

- **LangGraph:** multi-step graphs with branching, sub-agents, parallel fan-out, or when the team already uses
  it; prototypes where the checkpointer + interrupt primitives save weeks.
- **Hand-built:** when approval, authorization and audit are product features (queryable, multi-tenant),
  and when exactly-once side effects and crash recovery must be explained and tested explicitly.

Either way, the rules are the same: authorize at execution time, never before-interrupt side effects without
idempotency, and treat the model's output as an untrusted proposal.

## Not done
- A persistent checkpointer (Postgres/SQLite) and a real crash-mid-tool test on the LangGraph side.
- Taint escalation on LangGraph (it would be another custom check in the `tools` node).
- Running the full `smoke` eval suite through the LangGraph version (the suite drives `Runtime` directly).
