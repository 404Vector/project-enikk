"""Core → connector: messages to publish to the external interface."""

from __future__ import annotations

import uuid
from enum import StrEnum
from typing import Annotated

from pydantic import Field

from enikk_contracts.base import Contract
from enikk_contracts.common import ConversationRef, NonEmptyText
from enikk_contracts.tasks import WaitSeconds

MAX_ACK_BATCH = 100


class OutboundKind(StrEnum):
    REPLY = "reply"
    FAILURE = "failure"


class OutboundMessage(Contract):
    message_id: uuid.UUID
    task_id: uuid.UUID
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
