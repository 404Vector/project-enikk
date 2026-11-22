"""Postgres for the integration tests, in this order (docs/development/testing.md):

1. ``ENIKK_TEST_POSTGRES_DSN``: a disposable server on this machine (loopback only).
2. ``ENIKK_TEST_POSTGRES_BIN``: a directory with ``initdb`` and ``pg_ctl``; a temporary
   server is started on a private Unix socket and removed afterwards.
3. Otherwise a Docker container (testcontainers).

Each session creates its own database and drops it at the end; each test empties the
tables it may have written.
"""

from __future__ import annotations

import os
import secrets
import shutil
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Final

import psycopg
import pytest
from psycopg import sql
from pydantic import SecretStr
from sqlalchemy import delete, make_url
from testcontainers.community.postgres import PostgresContainer

from enikk_kernel.process import SubprocessRunner
from enikk_persistence_sql.engine import DatabaseConfig, create_engine, create_session_factory
from enikk_persistence_sql.migrate import upgrade
from enikk_persistence_sql.tables import (
    ApprovalRow,
    DeploymentWatchRow,
    DispatchRow,
    NoticeRow,
    OutboxRow,
    ReviewMessageRow,
    SessionRow,
    TaskRow,
    TranscriptEntryRow,
    VaultNoteRow,
)
from enikk_persistence_sql.uow import SqlUnitOfWorkFactory

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterator

# Same major version as the CloudNativePG cluster (ADR-0012 §1).
POSTGRES_IMAGE = "postgres:18-alpine"
DSN_ENV: Final = "ENIKK_TEST_POSTGRES_DSN"
BIN_ENV: Final = "ENIKK_TEST_POSTGRES_BIN"
LOOPBACK_HOSTS: Final = frozenset({"localhost", "127.0.0.1", "::1"})
LOCAL_USER: Final = "enikk"
LOCAL_PORT: Final = 5432
# Settings for a throwaway server: Unix socket only, no durability, small WAL.
LOCAL_SETTINGS: Final = (
    "listen_addresses = ''",
    "fsync = off",
    "synchronous_commit = off",
    "full_page_writes = off",
    "min_wal_size = 32MB",
    "max_wal_size = 64MB",
)


def _is_loopback(host: str) -> bool:
    return host in LOOPBACK_HOSTS or host.startswith("/")


def config_from_dsn(dsn: str) -> DatabaseConfig:
    """Server-level connection from a DSN. Refuses anything but this machine."""
    url = make_url(dsn)
    host = url.host or str(url.query.get("host", ""))
    if not _is_loopback(host):
        raise pytest.UsageError(
            f"{DSN_ENV} must point to a disposable Postgres on this machine "
            f"(localhost or a Unix socket), not {host!r}"
        )
    return DatabaseConfig(
        host=host,
        port=url.port or LOCAL_PORT,
        name=url.database or "postgres",
        user=url.username or LOCAL_USER,
        password=SecretStr(url.password or ""),
    )


@contextmanager
def _local_server(bindir: Path, root: Path) -> Iterator[DatabaseConfig]:
    runner = SubprocessRunner()
    data = root / "data"
    # Socket paths are limited to about 100 bytes, so keep this one short.
    socket_dir = Path(tempfile.mkdtemp(prefix="enikk-pg-"))
    try:
        runner.run(
            [
                str(bindir / "initdb"),
                "--pgdata",
                str(data),
                "--username",
                LOCAL_USER,
                "--auth",
                "trust",
                "--encoding",
                "UTF8",
                "--locale",
                "C",
                "--no-sync",
                "--no-instructions",
            ]
        )
        with (data / "postgresql.conf").open("a", encoding="utf-8") as conf:
            conf.write("\n".join((*LOCAL_SETTINGS, f"unix_socket_directories = '{socket_dir}'")))
            conf.write(f"\nport = {LOCAL_PORT}\n")
        pg_ctl = str(bindir / "pg_ctl")
        runner.run([pg_ctl, "start", "--pgdata", str(data), "--wait", "--log", str(root / "log")])
        try:
            yield DatabaseConfig(
                host=str(socket_dir),
                port=LOCAL_PORT,
                name="postgres",
                user=LOCAL_USER,
                password=SecretStr(""),
            )
        finally:
            runner.run(
                [pg_ctl, "stop", "--pgdata", str(data), "--mode", "immediate", "--wait"],
                check=False,
            )
    finally:
        shutil.rmtree(socket_dir, ignore_errors=True)


def _admin(server: DatabaseConfig) -> psycopg.Connection[tuple[object, ...]]:
    return psycopg.connect(
        host=server.host,
        port=server.port,
        user=server.user,
        password=server.password.get_secret_value() or None,
        dbname=server.name,
        autocommit=True,
    )


@pytest.fixture(scope="session")
def server(tmp_path_factory: pytest.TempPathFactory) -> Iterator[DatabaseConfig]:
    """A connection to a disposable Postgres server, not yet to a test database."""
    if dsn := os.environ.get(DSN_ENV):
        yield config_from_dsn(dsn)
    elif bindir := os.environ.get(BIN_ENV):
        with _local_server(Path(bindir), tmp_path_factory.mktemp("postgres")) as config:
            yield config
    else:
        with PostgresContainer(POSTGRES_IMAGE, driver=None) as container:
            yield DatabaseConfig(
                host=container.get_container_host_ip(),
                port=int(container.get_exposed_port(5432)),
                name=container.dbname,
                user=container.username,
                password=SecretStr(container.password),
            )


@pytest.fixture(scope="session")
def database(server: DatabaseConfig) -> Iterator[DatabaseConfig]:
    """A new, migrated database for this session, dropped at the end."""
    name = f"enikk_test_{secrets.token_hex(6)}"
    with _admin(server) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    try:
        config = server.model_copy(update={"name": name})
        upgrade(config)
        yield config
    finally:
        with _admin(server) as admin:
            admin.execute(
                sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name))
            )


@pytest.fixture
async def uow_factory(database: DatabaseConfig) -> AsyncIterator[SqlUnitOfWorkFactory]:
    engine = create_engine(database)
    async with engine.begin() as connection:
        for table in (
            TranscriptEntryRow,
            OutboxRow,
            DispatchRow,
            ApprovalRow,
            NoticeRow,
            DeploymentWatchRow,
            TaskRow,
            SessionRow,
            VaultNoteRow,
            ReviewMessageRow,
        ):
            await connection.execute(delete(table))
    try:
        yield SqlUnitOfWorkFactory(create_session_factory(engine))
    finally:
        await engine.dispose()
