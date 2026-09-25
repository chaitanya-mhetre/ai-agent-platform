.PHONY: check test lint type fmt up down integration eval
check: lint type test
test:
	uv run pytest -q
lint:
	uv run ruff check . && uv run ruff format --check .
type:
	uv run mypy
fmt:
	uv run ruff format . && uv run ruff check --fix .
up:
	docker compose up -d postgres redis
down:
	docker compose down
integration: up
	AGENTPLAT_TEST_PG_URL=postgresql+asyncpg://agent:agent@localhost:55434/agent \
	AGENTPLAT_TEST_REDIS_URL=redis://localhost:56381/0 uv run pytest -q -m integration
eval:
	uv run agent-eval run suites/core.yaml --out reports/
