"""The scheduler's clock (ADR-0039 S5): asks core to start what came due and to settle
what ended, every ``interval`` seconds. Core keeps one run per due time, so a second
clock (another replica, a restart) never doubles a run."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from enikk_core_client import CoreClient

_log = logging.getLogger(__name__)


class Clock:
    def __init__(self, core: CoreClient, interval: float) -> None:
        self._core = core
        self._interval = interval

    async def tick(self) -> None:
        try:
            ticked = await self._core.tick_schedules()
        except Exception as exc:
            _log.warning("tick failed: %s", exc)
            return
        if ticked.started or ticked.settled:
            _log.info("tick: started %d, settled %d", ticked.started, ticked.settled)

    async def run(self) -> None:
        while True:
            await self.tick()
            await asyncio.sleep(self._interval)
