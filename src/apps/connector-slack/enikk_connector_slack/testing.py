"""In-memory core and Slack for tests."""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from enikk_connector_slack.config import Routes, SlackConfig
from enikk_contracts.events import EventAccepted
from enikk_contracts.outbox import OutboxAcked, OutboxBatch
from enikk_core_client import CoreNotFoundError

if TYPE_CHECKING:
    from enikk_connector_slack.events import MessageRef
    from enikk_contracts.events import InboundEvent
    from enikk_contracts.outbox import OutboundMessage, OutboxAck, OutboxPoll

OWNER = "U0OWNER"
BOT = "U0BOT"
CONFIG = SlackConfig(owner=OWNER, routes=Routes(direct_message="assistant", mention="helper"))


class FakeCore:
    def __init__(self) -> None:
        self.events: list[InboundEvent] = []
        self.pending: list[OutboundMessage] = []
        self.acked: list[uuid.UUID] = []
        self.fail_with: Exception | None = None
        self.sessions: set[str] = set()

    async def submit_event(self, event: InboundEvent) -> EventAccepted:
        if self.fail_with is not None:
            raise self.fail_with
        thread = event.conversation.thread
        if event.continue_only and thread not in self.sessions:
            raise CoreNotFoundError("no conversation")
        self.sessions.add(thread)
        duplicate = any(e.event_id == event.event_id for e in self.events)
        if not duplicate:
            self.events.append(event)
        return EventAccepted(
            task_id=uuid.uuid4(), session_id=uuid.uuid4(), agent=event.agent, duplicate=duplicate
        )

    async def poll_outbox(self, poll: OutboxPoll) -> OutboxBatch:
        return OutboxBatch(messages=tuple(self.pending[: poll.limit]))

    async def ack_outbox(self, ack: OutboxAck) -> OutboxAcked:
        self.acked.extend(ack.message_ids)
        self.pending = [m for m in self.pending if m.message_id not in ack.message_ids]
        return OutboxAcked(acknowledged=len(ack.message_ids))


class FakeSlack:
    def __init__(self) -> None:
        self.posts: list[tuple[str, str, str]] = []
        self.reactions: list[tuple[str, str, str]] = []
        self.fail_posts: list[Exception] = []

    async def post_reply(self, channel: str, thread: str, text: str) -> None:
        if self.fail_posts:
            raise self.fail_posts.pop(0)
        self.posts.append((channel, thread, text))

    async def react(self, message: MessageRef, emoji: str) -> None:
        self.reactions.append(("+", message.ts, emoji))

    async def unreact(self, message: MessageRef, emoji: str) -> None:
        self.reactions.append(("-", message.ts, emoji))
