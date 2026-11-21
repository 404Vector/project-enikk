from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from pydantic import SecretStr
from sqlalchemy import delete
from testcontainers.community.postgres import PostgresContainer

from enikk_persistence_sql.engine import DatabaseConfig, create_engine, create_session_factory
from enikk_persistence_sql.migrate import upgrade
from enikk_persistence_sql.tables import OutboxRow, SessionRow, TaskRow, TranscriptEntryRow
from enikk_persistence_sql.uow import SqlUnitOfWorkFactory

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterator

# Same major version as the CloudNativePG cluster (ADR-0012 §1).
POSTGRES_IMAGE = "postgres:18-alpine"


@pytest.fixture(scope="session")
def database() -> Iterator[DatabaseConfig]:
    with PostgresContainer(POSTGRES_IMAGE, driver=None) as container:
        config = DatabaseConfig(
            host=container.get_container_host_ip(),
            port=int(container.get_exposed_port(5432)),
            name=container.dbname,
            user=container.username,
            password=SecretStr(container.password),
        )
        upgrade(config)
        yield config


@pytest.fixture
async def uow_factory(database: DatabaseConfig) -> AsyncIterator[SqlUnitOfWorkFactory]:
    engine = create_engine(database)
    async with engine.begin() as connection:
        for table in (TranscriptEntryRow, OutboxRow, TaskRow, SessionRow):
            await connection.execute(delete(table))
    try:
        yield SqlUnitOfWorkFactory(create_session_factory(engine))
    finally:
        await engine.dispose()
