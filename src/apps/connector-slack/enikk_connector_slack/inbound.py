"""Slack events → core events."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Final

from enikk_connector_slack.events import MessageRef
from enikk_core_client import CoreApiError, CoreNotFoundError, CoreUnavailableError
from enikk_kernel.models import FrozenModel

if TYPE_CHECKING:
    from enikk_connector_slack.events import EventCallback
    from enikk_connector_slack.ports import CoreApi, SlackApi
    from enikk_connector_slack.routing import Router

RECEIVED: Final = "eyes"
"""Shown while the task runs."""
WAITING: Final = "hourglass_flowing_sand"
"""Shown from acceptance until a worker takes the task (ADR-0037 3b)."""

_log = logging.getLogger(__name__)


class Decision(FrozenModel):
    """``acknowledge`` tells Slack the event is settled; otherwise Slack redelivers it."""

    acknowledge: bool
    received: MessageRef | None = None


class InboundHandler:
    def __init__(
        self, core: CoreApi, slack: SlackApi, router: Router, submit_timeout: float
    ) -> None:
        self._core = core
        self._slack = slack
        self._router = router
        self._timeout = submit_timeout

    async def handle(self, callback: EventCallback) -> Decision:
        event = self._router.route(callback.event)
        if event is None:
            return Decision(acknowledge=True)
        try:
            async with asyncio.timeout(self._timeout):
                accepted = await self._core.submit_event(event)
        except CoreNotFoundError:
            # A thread reply where the bot has no conversation.
            return Decision(acknowledge=True)
        except (CoreUnavailableError, TimeoutError) as exc:
            _log.warning(
                "core did not take event %s; Slack will redeliver: %s", event.event_id, exc
            )
            return Decision(acknowledge=False)
        except CoreApiError as exc:
            _log.error("core rejected event %s: %s", event.event_id, exc)
            return Decision(acknowledge=True)
        if accepted.duplicate:
            return Decision(acknowledge=True)
        _log.info(
            "event %s queued as task %s (%s)", event.event_id, accepted.task_id, accepted.agent
        )
        return Decision(acknowledge=True, received=MessageRef.parse(event.event_id))

    async def mark_received(self, decision: Decision) -> None:
        """Runs after the acknowledgement, so Slack's 3-second limit does not include it."""
        if decision.received is not None:
            await self._slack.react(decision.received, WAITING)
