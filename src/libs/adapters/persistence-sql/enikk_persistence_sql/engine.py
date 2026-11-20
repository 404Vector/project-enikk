"""Database connection configuration and engine creation."""

from __future__ import annotations

from typing import Annotated

from pydantic import Field, SecretStr
from sqlalchemy import URL
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from enikk_kernel.models import FrozenModel

DRIVER = "postgresql+psycopg"


class DatabaseConfig(FrozenModel):
    host: Annotated[str, Field(min_length=1)]
    port: Annotated[int, Field(ge=1, le=65535)] = 5432
    name: Annotated[str, Field(min_length=1)]
    user: Annotated[str, Field(min_length=1)]
    password: SecretStr
    pool_size: Annotated[int, Field(ge=1, le=50)] = 5

    def url(self) -> URL:
        return URL.create(
            DRIVER,
            username=self.user,
            password=self.password.get_secret_value(),
            host=self.host,
            port=self.port,
            database=self.name,
        )


def create_engine(config: DatabaseConfig) -> AsyncEngine:
    return create_async_engine(config.url(), pool_size=config.pool_size, pool_pre_ping=True)


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
