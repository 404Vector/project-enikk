"""Alembic entry points. Schema changes happen only through these migrations (I-26)."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext

from enikk_persistence_sql.engine import create_engine
from enikk_persistence_sql.tables import Base

if TYPE_CHECKING:
    from collections.abc import Callable

    from sqlalchemy import Connection

    from enikk_persistence_sql.engine import DatabaseConfig

MIGRATIONS_DIR = Path(__file__).parent / "migrations"


def _alembic_config(connection: Connection) -> Config:
    config = Config()
    config.set_main_option("script_location", str(MIGRATIONS_DIR))
    config.attributes["connection"] = connection
    return config


def upgrade(database: DatabaseConfig, revision: str = "head") -> None:
    """Apply migrations up to ``revision``."""
    asyncio.run(_run(database, lambda conn: command.upgrade(_alembic_config(conn), revision)))


def pending_schema_changes(database: DatabaseConfig) -> list[str]:
    """Describe differences between the migrated schema and the ORM models."""
    found: list[str] = []

    def collect(connection: Connection) -> None:
        context = MigrationContext.configure(connection, opts={"compare_type": True})
        found.extend(repr(diff) for diff in compare_metadata(context, Base.metadata))

    asyncio.run(_run(database, collect))
    return found


async def _run(database: DatabaseConfig, action: Callable[[Connection], None]) -> None:
    engine = create_engine(database)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(action)
    finally:
        await engine.dispose()
