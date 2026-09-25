"""Run Alembic migrations from Python (API/worker startup, tests, the CLI).

On PostgreSQL we take a transaction-scoped advisory lock first, so an API and a
worker starting at the same time can't both run DDL; the loser waits, then finds
the database already at head and does nothing.
"""

from __future__ import annotations

from pathlib import Path

import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from sqlalchemy.engine import Connection

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"
INITIAL_REVISION = "0001"
_LOCK_KEY = 0x61676E74  # "agnt": any constant works, it just has to be shared


def alembic_config() -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    return cfg


def _lock(conn: Connection) -> None:
    if conn.dialect.name == "postgresql":
        conn.execute(sa.text("SELECT pg_advisory_xact_lock(:k)"), {"k": _LOCK_KEY})


def upgrade(conn: Connection, revision: str = "head") -> None:
    _lock(conn)
    cfg = alembic_config()
    cfg.attributes["connection"] = conn
    _adopt_legacy_schema(conn, cfg)
    command.upgrade(cfg, revision)


def _adopt_legacy_schema(conn: Connection, cfg: Config) -> None:
    """Databases created by the old `metadata.create_all` have the 0001 tables but no
    alembic_version row. Stamp them at 0001 instead of failing on CREATE TABLE."""
    tables = set(sa.inspect(conn).get_table_names())
    if "agents" in tables and "alembic_version" not in tables:
        command.stamp(cfg, INITIAL_REVISION)


def downgrade(conn: Connection, revision: str = "base") -> None:
    _lock(conn)
    cfg = alembic_config()
    cfg.attributes["connection"] = conn
    command.downgrade(cfg, revision)


def current_revision(conn: Connection) -> str | None:
    return MigrationContext.configure(conn).get_current_revision()
