"""What the connector needs from core and from Slack."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

from enikk_kernel.errors import EnikkError
from enikk_kernel.models import FrozenModel

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
    from enikk_contracts.deployments import RevertRequest, RevertResult
    from enikk_contracts.events import EventAccepted, InboundEvent
    from enikk_contracts.outbox import (
        OutboxAck,
        OutboxAcked,
        OutboxBatch,
        OutboxFile,
        OutboxPoll,
    )
    from enikk_contracts.prompts import (
        PromotionDecision,
        PromotionLinked,
        PromotionPosted,
        PromotionResult,
    )
    from enikk_contracts.tasks import CancelResult
    from enikk_contracts.thread_actions import SlackWorkspaceReceived, SlackWorkspaceReport
    from enikk_contracts.threads import ReviewThreadLinked, ReviewThreadOpened


class CoreApi(Protocol):
    async def submit_event(self, event: InboundEvent) -> EventAccepted: ...

    async def poll_outbox(self, poll: OutboxPoll) -> OutboxBatch: ...

    async def ack_outbox(self, ack: OutboxAck) -> OutboxAcked: ...

    async def outbox_file(self, message_id: uuid.UUID) -> OutboxFile: ...

    async def poll_notices(self, poll: NoticePoll) -> NoticeBatch: ...

    async def ack_notices(self, ack: NoticeAck) -> NoticeAcked: ...

    async def decide(self, approval_id: uuid.UUID, request: DecisionRequest) -> DecisionResult: ...

    async def request_revert(self, request: RevertRequest) -> RevertResult: ...

    async def review_thread_opened(self, review: ReviewThreadOpened) -> ReviewThreadLinked: ...

    async def decide_promotion(
        self, promotion_id: uuid.UUID, decision: PromotionDecision
    ) -> PromotionResult: ...

    async def promotion_posted(
        self, promotion_id: uuid.UUID, posted: PromotionPosted
    ) -> PromotionLinked: ...

    async def cancel_task(self, task_id: uuid.UUID) -> CancelResult: ...

    async def report_slack_workspace(
        self, report: SlackWorkspaceReport
    ) -> SlackWorkspaceReceived: ...


class SlackDeliveryError(EnikkError):
    """A message could not be posted. ``transient`` errors are worth retrying."""

    def __init__(self, message: str, *, transient: bool) -> None:
        super().__init__(message)
        self.transient = transient


class WorkspaceView(FrozenModel):
    url: str
    emoji: tuple[str, ...] | None
    """Custom emoji names; ``None`` when the app may not list them."""


class SlackApi(Protocol):
    async def workspace(self) -> WorkspaceView:
        """The workspace's address and custom emoji (ADR-0042). Raises ``SlackDeliveryError``."""
        ...

    async def post_reply(self, channel: str, thread: str | None, text: str) -> None:
        """Post ``text`` in the thread, or as a new message without one.

        ``channel`` may be a user ID (their DM with the app). Raises ``SlackDeliveryError``."""
        ...

    async def post_message(
        self, channel: str, message: Message, thread: str | None = None
    ) -> PostedMessage:
        """Post a Block Kit message, in ``thread`` if given.

        ``channel`` may be a user ID (their DM with the app). Raises ``SlackDeliveryError``.
        """
        ...

    async def upload_text(
        self, channel: str, thread: str, filename: str, content: str, title: str
    ) -> None:
        """Attach a text file in the thread. Failures are logged, never raised."""
        ...

    async def upload_file(
        self, channel: str, thread: str, filename: str, content: bytes, title: str
    ) -> None:
        """Attach a file in the thread (ADR-0042). Raises ``SlackDeliveryError``."""
        ...

    async def update_message(
        self, channel: str, ts: str, text: str, blocks: list[dict[str, JsonValue]]
    ) -> None:
        """Replace a message. Failures are logged, never raised."""
        ...

    async def delete_message(self, channel: str, ts: str) -> None:
        """Delete one of the bot's messages. Failures are logged, never raised."""
        ...

    async def post_ephemeral(self, channel: str, user: str, text: str) -> None:
        """Show ``text`` to ``user`` alone in ``channel``. Failures are logged, never raised."""
        ...

    async def permalink(self, channel: str, ts: str) -> str | None:
        """The message's permalink. Failures are logged and give ``None``."""
        ...

    async def channel_id(self, name: str) -> str | None:
        """The ID of the channel called ``name`` (without ``#``) that the bot can see.
        Failures are logged and give ``None``."""
        ...

    async def react(self, message: MessageRef, emoji: str) -> bool:
        """Add a reaction; ``False`` only when the workspace has no such emoji.
        Other failures are logged, never raised."""
        ...

    async def unreact(self, message: MessageRef, emoji: str) -> None:
        """Remove a reaction. Failures are logged, never raised."""
        ...
