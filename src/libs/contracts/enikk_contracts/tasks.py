"""Runtime ↔ core: task leasing, heartbeats and results."""

from __future__ import annotations

import uuid
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import AwareDatetime, Field, StringConstraints

from enikk_contracts.agents import AgentDefinition
from enikk_contracts.base import Contract
from enikk_contracts.common import AgentName, Identifier, NonEmptyText

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


class LeaseRequest(Contract):
    """Wait up to ``wait_seconds`` for a task of the caller's engine."""

    wait_seconds: WaitSeconds = 20


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


DelegationText = Annotated[str, StringConstraints(min_length=1, max_length=20_000)]


class DelegateRequest(Contract):
    """A running agent hands a request to one of its delegates (ADR-0022, I-12).

    Only ``request`` crosses over: no conversation history. Delegating twice from the
    same task to the same agent returns the first delegation.
    """

    lease_id: uuid.UUID
    agent: AgentName
    request: DelegationText


class DelegationAccepted(Contract):
    task_id: uuid.UUID
    agent: AgentName
    duplicate: bool


class ClaimRequest(Contract):
    """An isolated worker claims the one task its Job was started for (ADR-0021)."""


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


class ResultAccepted(Contract):
    task_id: uuid.UUID
    status: TaskStatus
    duplicate: bool
