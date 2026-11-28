"""Connector → core: inbound user events."""

from __future__ import annotations

import uuid
from typing import Annotated

from pydantic import Field

from enikk_contracts.attachments import MAX_ATTACHMENTS, AttachmentInfo
from enikk_contracts.base import Contract
from enikk_contracts.common import AgentName, ConversationRef, Identifier, NonEmptyText


class InboundEvent(Contract):
    """A user message received by a connector.

    ``event_id`` is the idempotency key: delivering the same event twice
    creates one task. ``agent`` is the connector's routing choice and only
    applies when the conversation has no session yet. With ``continue_only``
    core accepts the event only into an existing session and answers
    "not found" otherwise. The conversation's connector must be the caller's.
    ``attachments`` are uploaded by the connector after core accepts the event
    (ADR-0042 2b).
    """

    event_id: Identifier
    conversation: ConversationRef
    user: Identifier
    text: NonEmptyText
    agent: AgentName
    continue_only: bool = False
    attachments: Annotated[tuple[AttachmentInfo, ...], Field(max_length=MAX_ATTACHMENTS)] = ()


class EventAccepted(Contract):
    task_id: uuid.UUID
    session_id: uuid.UUID
    agent: AgentName
    duplicate: bool
