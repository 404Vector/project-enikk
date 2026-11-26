"""Handing work to another agent and getting its result back (ADR-0047)."""

from __future__ import annotations

import uuid
from typing import Annotated, Final

from pydantic import StringConstraints

from enikk_contracts.base import Contract
from enikk_contracts.common import AgentName

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
