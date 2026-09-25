# Evaluation reports

| Report | What it is |
|---|---|
| `core-fake-fca4479.md` / `.json` | `core` suite (48 items × 3 repeats) against the **offline rule-based model**, full runtime policy |
| `core-fake-fca4479-ablation-no-taint.*` | Same run with the taint-escalation policy **disabled** (control experiment) |

**These are offline scripted baselines, not model quality claims.** The rule-based model is deterministic
(repeats never differ). It is deliberately gullible: it obeys `CALL tool {...}` instructions found inside tool
outputs. So these numbers measure **the runtime's safety controls**, not how smart any LLM is.

What the two reports show together, measured on git `fca4479`:
- The indirect-injection attack success rate is **0.375 with the taint policy disabled** and **0.0 with it enabled**. Same model, same 24 attack runs.
- Destructive actions executed without approval: 0 in both. The destructive → always-approve rule doesn't depend on taint.
- The remaining failures (`ts-004`, `ts-005`, `ha-001`) are the fake model's limits ("power of", "multiply X by Y", and not treating an HTTP 404 as a failure). They are not runtime bugs.

Real-model numbers are **TBD**. Run
`AGENTPLAT_GEMINI_API_KEY=... uv run agent-eval run suites/core.yaml --provider gemini --model <name> --repeat 3`
and commit the report together with the model name and date.
