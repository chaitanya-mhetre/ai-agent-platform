from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

import pytest

from agentplat.orchestrator import Runtime
from agentplat.providers.base import ModelProvider
from agentplat.runtime.events import EventPublisher, InMemoryBus
from agentplat.store.models import Agent
from agentplat.store.sql import SqlStore, new_id
from agentplat.tools.builtin.calculator import Calculator
from agentplat.tools.builtin.notes import NotesRead, NotesWrite
from agentplat.tools.registry import ToolRegistry


@pytest.fixture
async def store(tmp_path: Path) -> AsyncIterator[SqlStore]:
    s = SqlStore.from_url(f"sqlite+aiosqlite:///{tmp_path / 'test.db'}")
    await s.create_schema()
    yield s
    await s.close()


@pytest.fixture
def registry(store: SqlStore) -> ToolRegistry:
    return ToolRegistry([Calculator(), NotesWrite(store), NotesRead(store)])


@pytest.fixture
def bus() -> InMemoryBus:
    return InMemoryBus()


MakeRuntime = Callable[..., Runtime]


@pytest.fixture
def make_runtime(store: SqlStore, registry: ToolRegistry, bus: InMemoryBus) -> MakeRuntime:
    def _make(provider: ModelProvider, **kw: Any) -> Runtime:
        reg = kw.pop("registry", registry)
        return Runtime(store, reg, lambda _a: provider, EventPublisher(store, bus), **kw)

    return _make


MakeAgent = Callable[..., Agent]


@pytest.fixture
def make_agent(store: SqlStore) -> Callable[..., Any]:
    async def _make(**kw: Any) -> Agent:
        fields: dict[str, Any] = {
            "id": new_id(),
            "tenant_id": "acme",
            "name": "test-agent",
            "system_prompt": "You are a test agent.",
            "allowed_tools": ["calculator", "notes_write", "notes_read"],
        }
        fields.update(kw)
        agent = Agent(**fields)
        await store.create_agent(agent)
        return agent

    return _make
