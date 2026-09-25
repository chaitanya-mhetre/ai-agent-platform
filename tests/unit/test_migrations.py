"""Alembic migrations: empty -> head -> base round trip, idempotency, drift, legacy adoption."""

from pathlib import Path

import sqlalchemy as sa
from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory

from agentplat.store import schema
from agentplat.store.migrate import MIGRATIONS_DIR, alembic_config
from agentplat.store.sql import SqlStore

APP_TABLES = set(schema.metadata.tables)


def _head() -> str:
    head = ScriptDirectory.from_config(alembic_config()).get_current_head()
    assert head
    return head


async def _tables(store: SqlStore) -> set[str]:
    async with store.engine.connect() as conn:
        return await conn.run_sync(lambda c: set(sa.inspect(c).get_table_names()))


async def test_upgrade_from_empty_creates_every_table(tmp_path: Path) -> None:
    store = SqlStore.from_url(f"sqlite+aiosqlite:///{tmp_path / 'm.db'}")
    try:
        assert await _tables(store) == set()
        await store.migrate()
        assert await _tables(store) == APP_TABLES | {"alembic_version"}
        assert await store.schema_revision() == _head()
    finally:
        await store.close()


async def test_upgrade_is_idempotent(tmp_path: Path) -> None:
    store = SqlStore.from_url(f"sqlite+aiosqlite:///{tmp_path / 'm.db'}")
    try:
        await store.migrate()
        await store.migrate()  # second run is a no-op, not a "table exists" error
        assert await store.schema_revision() == _head()
    finally:
        await store.close()


async def test_downgrade_to_base_removes_everything_and_upgrade_again(tmp_path: Path) -> None:
    store = SqlStore.from_url(f"sqlite+aiosqlite:///{tmp_path / 'm.db'}")
    try:
        await store.migrate()
        await store.downgrade("base")
        assert await _tables(store) <= {"alembic_version"}
        assert await store.schema_revision() is None
        await store.migrate()
        assert await _tables(store) == APP_TABLES | {"alembic_version"}
    finally:
        await store.close()


async def test_models_and_migrations_do_not_drift(tmp_path: Path) -> None:
    """If someone edits store/schema.py without adding a revision, this fails."""
    store = SqlStore.from_url(f"sqlite+aiosqlite:///{tmp_path / 'm.db'}")
    try:
        await store.migrate()

        def diff(conn: sa.Connection) -> list[object]:
            ctx = MigrationContext.configure(conn, opts={"compare_type": True})
            return list(compare_metadata(ctx, schema.metadata))

        async with store.engine.connect() as conn:
            assert await conn.run_sync(diff) == []
    finally:
        await store.close()


async def test_legacy_create_all_database_is_adopted(tmp_path: Path) -> None:
    """Databases from before migrations existed get stamped, not recreated."""
    store = SqlStore.from_url(f"sqlite+aiosqlite:///{tmp_path / 'm.db'}")
    try:
        async with store.engine.begin() as conn:
            await conn.run_sync(schema.metadata.create_all)
        await store.migrate()
        assert await store.schema_revision() == _head()
    finally:
        await store.close()


def test_every_revision_has_a_downgrade() -> None:
    script = ScriptDirectory(str(MIGRATIONS_DIR))
    for rev in script.walk_revisions():
        source = Path(rev.path).read_text()
        assert "def downgrade" in source and "pass\n" not in source.split("def downgrade")[1]
