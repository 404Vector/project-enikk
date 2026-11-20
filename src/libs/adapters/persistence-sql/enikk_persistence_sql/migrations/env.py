"""Alembic environment. Runs only on a connection supplied by ``enikk_persistence_sql.migrate``."""

from __future__ import annotations

from alembic import context

from enikk_persistence_sql.tables import Base

connection = context.config.attributes.get("connection")
if connection is None:
    raise RuntimeError("run migrations through enikk_persistence_sql.migrate")

context.configure(connection=connection, target_metadata=Base.metadata, compare_type=True)
with context.begin_transaction():
    context.run_migrations()
