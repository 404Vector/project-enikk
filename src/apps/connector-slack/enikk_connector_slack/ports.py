"""What the connector needs from core and from Slack."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

from enikk_kernel.errors import EnikkError

if TYPE_CHECKING:
    from enikk_connector_slack.events import MessageRef
    from enikk_contracts.events import EventAccepted, InboundEvent
    from enikk_contracts.outbox import OutboxAck, OutboxAcked, OutboxBatch, OutboxPoll


class CoreApi(Protocol):
    async def submit_event(self, event: InboundEvent) -> EventAccepted: ...

    async def poll_outbox(self, poll: OutboxPoll) -> OutboxBatch: ...

    async def ack_outbox(self, ack: OutboxAck) -> OutboxAcked: ...


class SlackDeliveryError(EnikkError):
    """A message could not be posted. ``transient`` errors are worth retrying."""

    def __init__(self, message: str, *, transient: bool) -> None:
        super().__init__(message)
        self.transient = transient


class SlackApi(Protocol):
    async def post_reply(self, channel: str, thread: str, text: str) -> None:
        """Post ``text`` in the thread. Raises ``SlackDeliveryError``."""
        ...

    async def react(self, message: MessageRef, emoji: str) -> None:
        """Add a reaction. Failures are logged, never raised."""
        ...

    async def unreact(self, message: MessageRef, emoji: str) -> None:
        """Remove a reaction. Failures are logged, never raised."""
        ...
