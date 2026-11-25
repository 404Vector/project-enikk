"""Tells core what it cannot ask Slack itself: the workspace address and custom emoji.

Core builds message links and answers the emoji list from the latest report (ADR-0042).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import TYPE_CHECKING

from pydantic import ValidationError

from enikk_connector_slack.ports import SlackDeliveryError
from enikk_contracts.thread_actions import MAX_EMOJI, SlackWorkspaceReport
from enikk_core_client import CoreUnavailableError

if TYPE_CHECKING:
    from enikk_connector_slack.ports import CoreApi, SlackApi

_log = logging.getLogger(__name__)


class WorkspaceReporter:
    def __init__(self, core: CoreApi, slack: SlackApi, interval_seconds: float) -> None:
        self._core = core
        self._slack = slack
        self._interval = interval_seconds

    async def run(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            await self.report_once()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), self._interval)

    async def report_once(self) -> bool:
        """Report once; failures are logged and the next round tries again."""
        try:
            view = await self._slack.workspace()
            emoji = None if view.emoji is None else view.emoji[:MAX_EMOJI]
            report = SlackWorkspaceReport.model_validate(
                {"url": view.url, "emoji": emoji}, strict=False
            )
            received = await self._core.report_slack_workspace(report)
        except (SlackDeliveryError, CoreUnavailableError, ValidationError) as exc:
            _log.warning("could not report the workspace: %s", exc)
            return False
        if received.emoji is None:
            _log.info("custom emoji unknown: the app may lack emoji:read")
        return True
