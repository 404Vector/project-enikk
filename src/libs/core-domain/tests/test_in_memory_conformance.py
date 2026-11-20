from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from enikk_core_domain.testing import CONFORMANCE_CHECKS, InMemoryDatabase

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from enikk_core_domain.ports import UnitOfWorkFactory

pytestmark = pytest.mark.anyio


@pytest.mark.parametrize("check", CONFORMANCE_CHECKS, ids=lambda c: c.__name__)
async def test_in_memory_unit_of_work_conforms(
    check: Callable[[UnitOfWorkFactory], Awaitable[None]],
) -> None:
    await check(InMemoryDatabase().unit_of_work)
