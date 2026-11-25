"""core outbox → Slack threads."""

from __future__ import annotations

import asyncio
import base64
import logging
from typing import TYPE_CHECKING, Final

from enikk_connector_slack import blocks
from enikk_connector_slack.events import MessageRef
from enikk_connector_slack.inbound import RECEIVED, WAITING
from enikk_connector_slack.ports import SlackDeliveryError
from enikk_contracts.outbox import OutboundKind, OutboxAck, OutboxPoll
from enikk_core_client import CoreNotFoundError, CoreUnavailableError
from enikk_kernel.models import FrozenModel

if TYPE_CHECKING:
    import uuid
    from collections.abc import Awaitable, Callable

    from enikk_connector_slack.events import PostedMessage
    from enikk_connector_slack.ports import CoreApi, SlackApi
    from enikk_contracts.outbox import OutboundMessage

    Sleep = Callable[[float], Awaitable[None]]

REPLIED: Final = "white_check_mark"
FAILED: Final = "x"
CANCELLED: Final = "no_entry_sign"
FAILURE_PREFIX: Final = ":warning: 요청을 처리하지 못했습니다.\n"
FILE_FAILED: Final = ":warning: 파일 {name} 을 올리지 못했습니다."

_log = logging.getLogger(__name__)


class RelayPolicy(FrozenModel):
    wait_seconds: int
    batch: int
    backoff_max_seconds: float


class OutboxRelay:
    """Posts each outbox message to its thread, then acknowledges it to core.

    Delivery is at least once: a crash between posting and acknowledging posts again.
    """

    def __init__(
        self,
        core: CoreApi,
        slack: SlackApi,
        policy: RelayPolicy,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        self._core = core
        self._slack = slack
        self._policy = policy
        self._sleep = sleep
        self._buttons: dict[uuid.UUID, PostedMessage] = {}
        """[취소] messages of running tasks; lost on restart, then the press says "finished"."""

    async def run(self, stop: asyncio.Event) -> None:
        delay = 1.0
        while not stop.is_set():
            try:
                await self.relay_once()
            except (CoreUnavailableError, SlackDeliveryError) as exc:
                _log.warning("outbox relay paused for %.0fs: %s", delay, exc)
                await self._sleep(delay)
                delay = min(delay * 2, self._policy.backoff_max_seconds)
            else:
                delay = 1.0

    async def relay_once(self) -> int:
        """Deliver one batch. Returns how many messages were acknowledged."""
        poll = OutboxPoll(wait_seconds=self._policy.wait_seconds, limit=self._policy.batch)
        batch = await self._core.poll_outbox(poll)
        settled: list[uuid.UUID] = []
        try:
            for message in batch.messages:
                await self._deliver(message)
                settled.append(message.message_id)
        finally:
            if settled:
                await self._core.ack_outbox(OutboxAck(message_ids=tuple(settled)))
        return len(settled)

    async def _deliver(self, message: OutboundMessage) -> None:
        if message.kind is OutboundKind.STARTED:
            await self._started(message)
            return
        if message.kind is OutboundKind.FILE:
            await self._file(message)
            return
        if message.kind is OutboundKind.REACTION:
            source = MessageRef.parse(message.source_event_id)
            if source is not None:
                await self._slack.react(source, message.text)
            return
        conversation = message.conversation
        replied = message.kind is OutboundKind.REPLY
        try:
            await self._slack.post_reply(conversation.channel, conversation.thread, _text(message))
        except SlackDeliveryError as exc:
            if exc.transient:
                raise
            _log.error(
                "dropped message %s for task %s: %s", message.message_id, message.task_id, exc
            )
            replied = False
        button = self._buttons.pop(message.task_id, None)
        if button is not None:
            await self._slack.delete_message(button.channel, button.ts)
        source = MessageRef.parse(message.source_event_id)
        if source is not None:
            await self._slack.unreact(source, WAITING)
            await self._slack.unreact(source, RECEIVED)
            await self._slack.react(source, _mark(message.kind, replied=replied))

    async def _file(self, message: OutboundMessage) -> None:
        """Attach the message's file in its thread (ADR-0042); say so when Slack refuses it."""
        conversation = message.conversation
        try:
            file = await self._core.outbox_file(message.message_id)
        except CoreNotFoundError:
            _log.error("no file for outbox message %s", message.message_id)
            return
        try:
            await self._slack.upload_file(
                conversation.channel,
                conversation.thread,
                file.filename,
                base64.b64decode(file.content_base64),
                message.text,
            )
        except SlackDeliveryError as exc:
            if exc.transient:
                raise
            _log.error("dropped file %s for task %s: %s", file.filename, message.task_id, exc)
            await self._slack.post_reply(
                conversation.channel, conversation.thread, FILE_FAILED.format(name=file.filename)
            )

    async def _started(self, message: OutboundMessage) -> None:
        """Mark the request as running and show the [취소] button (ADR-0037 3b)."""
        if message.task_id in self._buttons:
            return
        source = MessageRef.parse(message.source_event_id)
        if source is not None:
            await self._slack.unreact(source, WAITING)
            await self._slack.react(source, RECEIVED)
        conversation = message.conversation
        try:
            posted = await self._slack.post_message(
                conversation.channel,
                blocks.running(str(message.task_id)),
                thread=conversation.thread,
            )
        except SlackDeliveryError as exc:
            if exc.transient:
                raise
            _log.warning("no cancel button for task %s: %s", message.task_id, exc)
            return
        self._buttons[message.task_id] = posted


def _mark(kind: OutboundKind, *, replied: bool) -> str:
    if kind is OutboundKind.CANCELLED:
        return CANCELLED
    return REPLIED if replied else FAILED


def _text(message: OutboundMessage) -> str:
    if message.kind is OutboundKind.FAILURE:
        return FAILURE_PREFIX + message.text
    return message.text
