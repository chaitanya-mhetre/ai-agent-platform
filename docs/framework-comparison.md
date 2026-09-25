# Hand-written runtime vs. LangGraph

**Status: design comparison only.** The spec (M8) calls for re-implementing one scenario in LangGraph and
comparing the two hands-on. That part has **not been built yet** (tracked in the README roadmap). This doc
compares the designs, using the LangGraph documentation (checked Sep 2026 through the official docs:
`docs.langchain.com/oss/python/langgraph`).

## How the concepts map

| Concern | agentplat (this repo) | LangGraph |
|---|---|---|
| Control flow | Explicit `while` loop in `Runtime._drive`: model → guard → executor → observation | A `StateGraph` of nodes and edges (e.g. `agent` node ↔ `tools` node with a conditional edge) |
| State | `Run` row: messages, counters, `tainted` flag, status (explicit state machine in `state.py`) | Typed `State` dict with reducers (e.g. `Annotated[list, add]`), versioned per step |
| Durability | Persist the proposal *before* executing; persist every observation; leases + reclaimer for dead workers | A **checkpointer** (e.g. `InMemorySaver`, Postgres/Redis savers) records state at every super-step, keyed by `thread_id` |
| Resume after crash | Another worker re-derives unanswered calls from persisted messages; idempotency keys stop duplicate side effects | Invoke again with the same `thread_id` (and `None` input) to continue from the last checkpoint |
| Human approval | Guard returns `needs_approval` → `approvals` row → run → `awaiting_approval`; the approvals API re-queues the run | `interrupt(payload)` inside a node pauses the graph (needs a checkpointer); resume with `Command(resume=value)` |
| Retries / timeouts | `ToolExecutor`: per-tool timeout, retries only for transient errors | Node-level retry policies. Per the docs, `interrupt()` bypasses retry policies and error handlers |
| Authorization | Execution-time guard pipeline (schema → allow-list ∩ user permissions → policy → approval) | Not built in: you write it in the tools node (or wrap tools) |
| Prompt-injection policy | Taint flag on the run and escalation to approval | Not built in: same, custom node logic |
| Observability | Own span tree in Postgres + OTel export + Prometheus | Integrates with LangSmith; OTel possible through LangChain callbacks |
| Evaluation | `agent-eval`: deterministic suites, ablations | LangSmith evaluations (hosted), or your own harness |

## Trade-offs

**Why hand-write it here?** The project exists to show understanding of the *mechanics*: when state
gets persisted, why a proposal is saved before execution, how leases stop double execution, and where
authorization has to happen. With a framework, those decisions still exist, but they're buried in library defaults.

**Where LangGraph would win:**
- Complex graphs: branching sub-agents, parallel fan-out and map-reduce patterns are declarative instead of hand-coded.
- Batteries included: checkpointer backends, streaming modes, time-travel over checkpoints, a studio UI.
- Team familiarity: many teams already know it, so it's cheaper to hire and onboard for.

**Where the hand-written runtime wins:**
- Security controls are first-class and testable in isolation (`guards.py`, `security/`). In a framework they'd be glue code inside a node.
- Exactly-once side effects are explicit (idempotency key = f(run, call id, args)). A checkpointer alone doesn't give you this: a node that crashes after the side effect and before the checkpoint will re-run it.
- Only a few dependencies, and every line can be explained in an interview.

**A subtle point worth knowing (interview material):** with checkpoint-based resumption, the unit of replay is
the *node*. When an interrupted node resumes, it runs again from the start, so any side effect *before*
`interrupt()` in that node would repeat. The usual fix is the same one used here: put side effects after the
approval point, or make them idempotent.

## TODO (hands-on part)
- [ ] Re-implement the "destructive tool needs approval" scenario as a LangGraph `StateGraph` with a Postgres checkpointer.
- [ ] Run the same `smoke` suite through both and compare pass rate, lines of code, and behaviour on a crash mid-tool.
- [ ] Record what each needs in order to add taint escalation.
