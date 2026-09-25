# Contributing

1. `uv sync` and then `make check`. Lint, types and tests must be green.
2. Add a test for every behaviour change. Security-relevant changes (guards, SSRF, taint, redaction) also need
   an entry in `docs/threat-model.md`.
3. Changing agent behaviour (prompts, policy, planner)? Run `make eval` and compare it with the committed baseline:
   `uv run agent-eval compare reports/<baseline>.json reports/<new>.json`. The smoke suite must stay at 1.0.
4. Use conventional commits (`feat:`, `fix:`, `test:`, `docs:`, `refactor:`) and add a `CHANGELOG.md` entry.
5. Never commit secrets. `.env` is gitignored. CI runs gitleaks.

Integration tests: `make integration` starts Postgres on :55434 and Redis on :56381.
