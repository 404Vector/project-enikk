from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from enikk_core_domain.testing import CONFORMANCE_CHECKS
from enikk_persistence_sql.migrate import pending_schema_changes

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from enikk_core_domain.ports import UnitOfWorkFactory
    from enikk_persistence_sql.engine import DatabaseConfig
    from enikk_persistence_sql.uow import SqlUnitOfWorkFactory


@pytest.mark.anyio
@pytest.mark.parametrize("check", CONFORMANCE_CHECKS, ids=lambda c: c.__name__)
async def test_sql_unit_of_work_conforms(
    check: Callable[[UnitOfWorkFactory], Awaitable[None]],
    uow_factory: SqlUnitOfWorkFactory,
) -> None:
    await check(uow_factory)


def test_migrations_match_models(database: DatabaseConfig) -> None:
    assert pending_schema_changes(database) == []
