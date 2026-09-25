"""agentplat-db: apply, roll back and inspect schema migrations.

    agentplat-db upgrade            # to head
    agentplat-db downgrade 0001     # or `base`
    agentplat-db current
    agentplat-db revision "add foo" # autogenerate from store/schema.py (dev only)

Uses AGENTPLAT_DATABASE_URL unless --url is given.
"""

from __future__ import annotations

import asyncio
from typing import Annotated

import typer

from agentplat.config import Settings
from agentplat.store.sql import SqlStore

app = typer.Typer(help="Database migrations (Alembic).")
UrlOpt = Annotated[str | None, typer.Option(help="Database URL (default: from settings)")]


def _store(url: str | None) -> SqlStore:
    return SqlStore.from_url(url or Settings().database_url)


async def _with_store(url: str | None, action: str, revision: str) -> str | None:
    store = _store(url)
    try:
        if action == "upgrade":
            await store.migrate(revision)
        elif action == "downgrade":
            await store.downgrade(revision)
        return await store.schema_revision()
    finally:
        await store.close()


@app.command()
def upgrade(revision: str = "head", url: UrlOpt = None) -> None:
    typer.echo(f"at revision: {asyncio.run(_with_store(url, 'upgrade', revision))}")


@app.command()
def downgrade(revision: str, url: UrlOpt = None) -> None:
    typer.echo(f"at revision: {asyncio.run(_with_store(url, 'downgrade', revision))}")


@app.command()
def current(url: UrlOpt = None) -> None:
    typer.echo(f"at revision: {asyncio.run(_with_store(url, 'current', ''))}")


@app.command()
def revision(message: str, url: UrlOpt = None) -> None:
    """Autogenerate a new revision by diffing store/schema.py against an up-to-date DB."""
    import sqlalchemy as sa
    from alembic import command

    from agentplat.store.migrate import alembic_config

    async def run() -> None:
        store = _store(url)
        try:
            await store.migrate()

            def gen(conn: sa.Connection) -> None:
                cfg = alembic_config()
                cfg.attributes["connection"] = conn
                command.revision(cfg, message=message, autogenerate=True)

            async with store.engine.begin() as conn:
                await conn.run_sync(gen)
        finally:
            await store.close()

    asyncio.run(run())
