"""The observer used when tracing is not configured."""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from enikk_runtime.model import EngineJob, EngineResult


class _Ignored:
    def succeeded(self, result: EngineResult) -> None:
        return None

    def failed(self, reason: str) -> None:
        return None


class NullObserver:
    @asynccontextmanager
    async def observe(self, job: EngineJob) -> AsyncIterator[_Ignored]:
        yield _Ignored()
