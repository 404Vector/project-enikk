"""Connector → core: inbound user events."""

from __future__ import annotations

import uuid

from enikk_contracts.base import Contract
from enikk_contracts.common import AgentName, ConversationRef, Identifier, NonEmptyText


class InboundEvent(Contract):
    """A user message received by a connector.

    ``event_id`` is the idempotency key: delivering the same event twice
    creates one task. ``agent`` is the connector's routing choice and only
    applies when the conversation has no session yet.
    """

    event_id: Identifier
    conversation: ConversationRef
    user: Identifier
    text: NonEmptyText
    agent: AgentName


class EventAccepted(Contract):
    task_id: uuid.UUID
    session_id: uuid.UUID
    agent: AgentName
    duplicate: bool
