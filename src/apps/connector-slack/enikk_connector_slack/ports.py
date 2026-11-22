"""What the connector needs from core and from Slack."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

from enikk_kernel.errors import EnikkError

if TYPE_CHECKING:
    import uuid

    from pydantic import JsonValue

    from enikk_connector_slack.blocks import Message
    from enikk_connector_slack.events import MessageRef, PostedMessage
    from enikk_contracts.approvals import (
        DecisionRequest,
        DecisionResult,
        NoticeAck,
        NoticeAcked,
        NoticeBatch,
        NoticePoll,
    )
    from enikk_contracts.events import EventAccepted, InboundEvent
    from enikk_contracts.outbox import OutboxAck, OutboxAcked, OutboxBatch, OutboxPoll


class CoreApi(Protocol):
    async def submit_event(self, event: InboundEvent) -> EventAccepted: ...

    async def poll_outbox(self, poll: OutboxPoll) -> OutboxBatch: ...

    async def ack_outbox(self, ack: OutboxAck) -> OutboxAcked: ...

    async def poll_notices(self, poll: NoticePoll) -> NoticeBatch: ...

    async def ack_notices(self, ack: NoticeAck) -> NoticeAcked: ...

    async def decide(self, approval_id: uuid.UUID, request: DecisionRequest) -> DecisionResult: ...


class SlackDeliveryError(EnikkError):
    """A message could not be posted. ``transient`` errors are worth retrying."""

    def __init__(self, message: str, *, transient: bool) -> None:
        super().__init__(message)
        self.transient = transient


class SlackApi(Protocol):
    async def post_reply(self, channel: str, thread: str, text: str) -> None:
        """Post ``text`` in the thread. Raises ``SlackDeliveryError``."""
        ...

    async def post_message(self, channel: str, message: Message) -> PostedMessage:
        """Post a Block Kit message. ``channel`` may be a user ID (their DM with the app)."""
        ...

    async def upload_text(
        self, channel: str, thread: str, filename: str, content: str, title: str
    ) -> None:
        """Attach a text file in the thread. Failures are logged, never raised."""
        ...

    async def update_message(
        self, channel: str, ts: str, text: str, blocks: list[dict[str, JsonValue]]
    ) -> None:
        """Replace a message. Failures are logged, never raised."""
        ...

    async def react(self, message: MessageRef, emoji: str) -> None:
        """Add a reaction. Failures are logged, never raised."""
        ...

    async def unreact(self, message: MessageRef, emoji: str) -> None:
        """Remove a reaction. Failures are logged, never raised."""
        ...
