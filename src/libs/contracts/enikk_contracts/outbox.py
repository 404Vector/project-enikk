"""Core → connector: messages to publish to the external interface."""

from __future__ import annotations

import uuid
from enum import StrEnum
from typing import Annotated

from pydantic import Field

from enikk_contracts.base import Contract
from enikk_contracts.common import ConversationRef, Identifier, NonEmptyText
from enikk_contracts.tasks import WaitSeconds
from enikk_contracts.thread_actions import Base64Text

MAX_ACK_BATCH = 100


class OutboundKind(StrEnum):
    REPLY = "reply"
    FAILURE = "failure"
    STARTED = "started"
    """A worker took the task; the connector shows it as running (ADR-0037 3b)."""
    CANCELLED = "cancelled"
    """The owner cancelled the thread's work."""
    FILE = "file"
    """A file for the thread; ``text`` is its title, the bytes come from ``OUTBOX_FILE``."""
    REACTION = "reaction"
    """An emoji on the message the task answers; ``text`` is the emoji name (ADR-0042)."""
    REMEMBERED = "remembered"
    """The task kept a memory note; shown as a mark on the message, ``text`` is the record."""
    MEMORY_UPDATED = "memory_updated"
    FORGOTTEN = "forgotten"


class OutboundMessage(Contract):
    """``source_event_id`` is the ``event_id`` of the inbound event the task answers."""

    message_id: uuid.UUID
    task_id: uuid.UUID
    source_event_id: Identifier
    conversation: ConversationRef
    kind: OutboundKind
    text: NonEmptyText


class OutboxPoll(Contract):
    """Wait up to ``wait_seconds`` for undelivered messages of the caller's connector."""

    wait_seconds: WaitSeconds = 20
    limit: Annotated[int, Field(ge=1, le=MAX_ACK_BATCH)] = 20


class OutboxBatch(Contract):
    """Messages stay pending, and are returned again, until acknowledged."""

    messages: tuple[OutboundMessage, ...]


class OutboxAck(Contract):
    message_ids: Annotated[tuple[uuid.UUID, ...], Field(min_length=1, max_length=MAX_ACK_BATCH)]


class OutboxAcked(Contract):
    acknowledged: Annotated[int, Field(ge=0)]


class OutboxFileQuery(Contract):
    """Ask for the bytes of a ``FILE`` message; the message is in the path."""


class OutboxFile(Contract):
    """The bytes of a ``FILE`` message, base64-encoded (ADR-0042)."""

    filename: Identifier
    content_base64: Base64Text
