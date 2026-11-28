"""Slack events → core events."""

from __future__ import annotations

import asyncio
import base64
import logging
import uuid
from typing import TYPE_CHECKING

from enikk_connector_slack.events import MessageRef, SlackFile
from enikk_connector_slack.marks import Mark, Marker, Reactions
from enikk_connector_slack.ports import SlackDeliveryError
from enikk_contracts.attachments import MAX_ATTACHMENT_BYTES, AttachmentUpload
from enikk_core_client import CoreApiError, CoreNotFoundError, CoreUnavailableError
from enikk_kernel.models import FrozenModel

if TYPE_CHECKING:
    from enikk_connector_slack.events import EventCallback
    from enikk_connector_slack.ports import CoreApi, SlackApi
    from enikk_connector_slack.routing import Router


_log = logging.getLogger(__name__)


class Decision(FrozenModel):
    """``acknowledge`` tells Slack the event is settled; otherwise Slack redelivers it."""

    acknowledge: bool
    received: MessageRef | None = None
    task_id: uuid.UUID | None = None
    files: tuple[SlackFile, ...] = ()
    """Attachments to upload to core for ``task_id`` after the acknowledgement."""


class InboundHandler:
    def __init__(
        self,
        core: CoreApi,
        slack: SlackApi,
        router: Router,
        submit_timeout: float,
        marker: Marker | None = None,
    ) -> None:
        self._core = core
        self._slack = slack
        self._marker = Marker(slack, Reactions()) if marker is None else marker
        self._router = router
        self._timeout = submit_timeout
        self._uploads: set[asyncio.Task[None]] = set()

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
        return Decision(
            acknowledge=True,
            received=MessageRef.parse(event.event_id),
            task_id=accepted.task_id,
            files=callback.event.files[: len(event.attachments)],
        )

    async def mark_received(self, decision: Decision) -> None:
        """Runs after the acknowledgement, so Slack's 3-second limit does not include it."""
        if decision.received is not None:
            await self._marker.mark(decision.received, Mark.WAITING)
        if decision.task_id is not None and decision.files:
            upload = asyncio.create_task(self.upload_attachments(decision.task_id, decision.files))
            self._uploads.add(upload)
            upload.add_done_callback(self._uploads.discard)

    async def upload_attachments(self, task_id: uuid.UUID, files: tuple[SlackFile, ...]) -> None:
        """Download each attachment from Slack and hand it to core (ADR-0042 2b).

        A file that cannot be read is reported as failed, so the task does not wait for it."""
        for index, file in enumerate(files):
            try:
                content = await self._slack.download_file(file, MAX_ATTACHMENT_BYTES)
            except SlackDeliveryError as exc:
                _log.warning("task %s attachment %d not downloaded: %s", task_id, index, exc)
                upload = AttachmentUpload(failure=str(exc)[:200] or "download failed")
            else:
                upload = AttachmentUpload(content_base64=base64.b64encode(content).decode())
            try:
                await self._core.upload_attachment(task_id, index, upload)
            except CoreApiError as exc:
                _log.error("task %s attachment %d not uploaded: %s", task_id, index, exc)
