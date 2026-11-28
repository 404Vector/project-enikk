"""Handing work to another agent and getting its result back (ADR-0047), and asking
another agent for work with the owner's approval in the thread (ADR-0054)."""

from __future__ import annotations

import uuid
from enum import StrEnum
from typing import Annotated, Final, Literal

from pydantic import AwareDatetime, StringConstraints

from enikk_contracts.base import Contract
from enikk_contracts.common import AgentName, ConversationRef, Identifier

MAX_REQUEST_CHARS: Final = 20_000


def delegated_session_id(parent: uuid.UUID, agent: str) -> uuid.UUID:
    """The session that does ``agent``'s work for the thread session ``parent``: one per
    pair, so delegating again continues it. Runtime finds its workspace by this ID."""
    return uuid.uuid5(parent, agent)


def delegated_thread(thread: str, agent: str) -> str:
    """The conversation key of a delegated session, apart from its parent thread's."""
    return f"{thread}:{agent}"


class DelegateRequest(Contract):
    lease_id: uuid.UUID
    agent: AgentName
    request: Annotated[
        str, StringConstraints(min_length=1, max_length=MAX_REQUEST_CHARS, strip_whitespace=True)
    ]


class Delegated(Contract):
    agent: AgentName
    session_id: uuid.UUID
    task_id: uuid.UUID
    duplicate: bool
    """True when this task had already handed work to the agent; nothing new was queued."""


# ------------------------------------------- asking enikk, with the owner's approval (ADR-0054)

ASK_EVENT_PREFIX: Final = "ask:"
"""Source event IDs of the tasks an approved request queues: ``ask:<request>``."""

AskReason = Annotated[str, StringConstraints(min_length=1, max_length=500, strip_whitespace=True)]


class AskStatus(StrEnum):
    REQUESTED = "requested"
    APPROVED = "approved"
    """The owner allowed it; the asked agent's task is queued."""
    REJECTED = "rejected"
    EXPIRED = "expired"
    """Nobody decided before it expired; nothing was queued."""


class AskRequest(Contract):
    """Ask another agent (``asks`` of the definition) for work. Nothing runs until the
    owner presses [허가] on the card in this thread."""

    lease_id: uuid.UUID
    agent: AgentName
    request: Annotated[
        str, StringConstraints(min_length=1, max_length=MAX_REQUEST_CHARS, strip_whitespace=True)
    ]
    reason: AskReason


class AskCreated(Contract):
    request_id: uuid.UUID
    agent: AgentName
    expires_at: AwareDatetime


class AskCard(Contract):
    """The asking thread: may ``agent`` do this for ``requester``? With [허가]·[거절]."""

    kind: Literal["ask-requested"] = "ask-requested"
    request_id: uuid.UUID
    requester: AgentName
    agent: AgentName
    request: Annotated[str, StringConstraints(min_length=1, max_length=MAX_REQUEST_CHARS)]
    reason: AskReason
    origin: ConversationRef
    expires_at: AwareDatetime


class AskDecision(Contract):
    """The owner pressed a button. The connector has checked who pressed it (I-01)."""

    approve: bool
    decided_by: Identifier


class AskResult(Contract):
    status: AskStatus
    changed: bool
    """False for a repeated or late press."""
    detail: Annotated[str, StringConstraints(max_length=1_000)] = ""
