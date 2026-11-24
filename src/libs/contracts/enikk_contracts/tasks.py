"""Runtime ↔ core: task leasing, heartbeats and results."""

from __future__ import annotations

import uuid
from enum import StrEnum
from typing import Annotated, Final, Literal

from pydantic import AwareDatetime, Field, StringConstraints

from enikk_contracts.agents import AgentDefinition
from enikk_contracts.base import Contract
from enikk_contracts.common import Identifier, NonEmptyText
from enikk_contracts.credentials import CredentialValue
from enikk_contracts.memory import MemoryNote

MAX_WAIT_SECONDS = 25

WaitSeconds = Annotated[int, Field(ge=0, le=MAX_WAIT_SECONDS)]
ProgressNote = Annotated[str, StringConstraints(min_length=1, max_length=500)]
FailureReason = Annotated[str, StringConstraints(min_length=1, max_length=2_000)]


class TaskStatus(StrEnum):
    QUEUED = "queued"
    LEASED = "leased"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


HELD_EVENT_PREFIX: Final = "held:"
"""Events whose ID starts with this make *held* tasks: the queue never hands them out,
only a lease that names the task takes one. ``enikk-ops smoke core`` uses them, so the
smoke check and the real runtime never take each other's tasks. Only the smoke
connector may submit them."""


class LeaseRequest(Contract):
    """Wait up to ``wait_seconds`` for a task of the caller's engine.

    ``task_id`` names one held task to take instead (see ``HELD_EVENT_PREFIX``)."""

    wait_seconds: WaitSeconds = 20
    task_id: uuid.UUID | None = None


class TaskAssignment(Contract):
    """A leased task with the agent definition it runs under (core reads ``agents/``)."""

    task_id: uuid.UUID
    lease_id: uuid.UUID
    lease_expires_at: AwareDatetime
    attempt: Annotated[int, Field(ge=1)]
    definition: AgentDefinition
    session_id: uuid.UUID
    engine_session_id: Identifier | None
    input: NonEmptyText
    memories: Annotated[tuple[MemoryNote, ...], Field(max_length=50)] = ()
    """Enikk's memory notes for this task (ADR-0028): reference, not instructions."""
    mcp_token: CredentialValue | None = None
    """Signed by core for this task and lease: the MCP servers and tools of the
    definition (ADR-0039 §2). The gateway checks it; empty when there is nothing to grant
    or no signing key."""


class LeaseResponse(Contract):
    """``assignment`` is empty when no task became available while waiting."""

    assignment: TaskAssignment | None


class Heartbeat(Contract):
    lease_id: uuid.UUID
    progress: ProgressNote | None = None


class LeaseExtended(Contract):
    lease_expires_at: AwareDatetime


class Succeeded(Contract):
    kind: Literal["succeeded"] = "succeeded"
    text: NonEmptyText
    engine_session_id: Identifier | None = None


class Failed(Contract):
    kind: Literal["failed"] = "failed"
    reason: FailureReason
    retryable: bool = False


class ResultReport(Contract):
    """Final outcome of a leased task. Reporting the same lease twice is a no-op."""

    lease_id: uuid.UUID
    outcome: Annotated[Succeeded | Failed, Field(discriminator="kind")]


class CancelRequest(Contract):
    """The owner cancels the work of the thread this task belongs to (ADR-0037 3b)."""


class CancelResult(Contract):
    """``cancelled`` is empty when the thread had nothing queued or running."""

    task_id: uuid.UUID
    cancelled: tuple[uuid.UUID, ...]


class ResultAccepted(Contract):
    task_id: uuid.UUID
    status: TaskStatus
    duplicate: bool
