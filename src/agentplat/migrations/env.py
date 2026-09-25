"""Alembic environment.

Two ways in:
- programmatic (`SqlStore.migrate()`): the caller passes an open *sync* connection
  through `config.attributes["connection"]`, so migrations run inside the app's
  own engine/transaction (and behind its advisory lock on Postgres);
- CLI (`agentplat-db upgrade`, or plain `alembic` with alembic.ini): we build an
  async engine from `-x url=...` or AGENTPLAT_DATABASE_URL.
"""

from __future__ import annotations

import asyncio

from alembic import context
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import create_async_engine

from agentplat.store.schema import metadata

config = context.config
target_metadata = metadata


def _configure(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        # SQLite can't ALTER most things; batch mode rebuilds the table instead.
        render_as_batch=connection.dialect.name == "sqlite",
    )


def _run(connection: Connection) -> None:
    _configure(connection)
    with context.begin_transaction():
        context.run_migrations()


def _url() -> str:
    url = context.get_x_argument(as_dictionary=True).get("url")
    if url:
        return url
    from agentplat.config import Settings

    return Settings().database_url


async def _run_async() -> None:
    engine = create_async_engine(_url())
    async with engine.connect() as conn:
        await conn.run_sync(_run)
        await conn.commit()
    await engine.dispose()


def run_offline() -> None:
    context.configure(url=_url(), target_metadata=target_metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()


if context.is_offline_mode():
    run_offline()
else:
    conn = config.attributes.get("connection")
    if conn is not None:
        _run(conn)
    else:
        asyncio.run(_run_async())
