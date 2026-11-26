"""Entities and their state machines.

Entities are immutable: every transition returns a new, fully re-validated
instance, so an entity in an inconsistent state cannot exist.
"""

from __future__ import annotations

import uuid
from enum import StrEnum
from typing import TYPE_CHECKING, Annotated, Self

from pydantic import AwareDatetime, Field, StringConstraints, model_validator

from enikk_contracts.agents import Execution
from enikk_contracts.approvals import OwnerNoticeBody
from enikk_contracts.common import AgentName, ConversationRef, Engine, Identifier, NonEmptyText
from enikk_contracts.outbox import OutboundKind
from enikk_contracts.tasks import FailureReason, ProgressNote, TaskStatus
from enikk_contracts.thread_actions import MAX_FILE_BYTES
from enikk_core_domain.errors import InvalidTransitionError, LeaseLostError
from enikk_kernel.models import FrozenModel

if TYPE_CHECKING:
    from datetime import datetime, timedelta

    from enikk_contracts.agents import AgentDefinition

TERMINAL_STATUSES = frozenset({TaskStatus.SUCCEEDED, TaskStatus.FAILED, TaskStatus.CANCELLED})


class Entity(FrozenModel):
    def _evolve(self, **changes: object) -> Self:
        """Return a copy with ``changes`` applied, validated like a new instance."""
        return type(self)(**{**dict(self), **changes})


class Session(Entity):
    """A conversation thread bound to one agent definition (I-11)."""

    id: uuid.UUID
    conversation: ConversationRef
    agent: AgentName
    engine: Engine
    engine_session_id: Identifier | None = None
    reply_to: ConversationRef | None = None
    """Where answers go when they belong elsewhere: a delegated session posts into the
    thread it was delegated from, without receiving that thread's messages (ADR-0022)."""
    created_at: AwareDatetime
    updated_at: AwareDatetime

    @classmethod
    def start(
        cls,
        *,
        session_id: uuid.UUID,
        conversation: ConversationRef,
        agent: AgentDefinition,
        now: datetime,
        reply_to: ConversationRef | None = None,
    ) -> Self:
        return cls(
            id=session_id,
            conversation=conversation,
            agent=agent.name,
            engine=agent.engine,
            reply_to=reply_to,
            created_at=now,
            updated_at=now,
        )

    @model_validator(mode="after")
    def _reply_through_own_connector(self) -> Self:
        if self.reply_to is not None and self.reply_to.connector is not self.conversation.connector:
            raise ValueError("a session replies only through its own connector")
        return self

    @property
    def outbox_conversation(self) -> ConversationRef:
        return self.reply_to or self.conversation

    def bind_engine_session(self, engine_session_id: str, now: datetime) -> Self:
        if engine_session_id == self.engine_session_id:
            return self
        return self._evolve(engine_session_id=engine_session_id, updated_at=now)


class Lease(FrozenModel):
    id: uuid.UUID
    expires_at: AwareDatetime

    def expired(self, now: datetime) -> bool:
        return now >= self.expires_at


class Task(Entity):
    """One unit of agent work. ``QUEUED`` tasks are the job queue (ADR-0012)."""

    id: uuid.UUID
    session_id: uuid.UUID
    source_event_id: Identifier
    agent: AgentName
    engine: Engine
    execution: Execution = Execution.SHARED
    input: NonEmptyText
    status: TaskStatus
    attempt: Annotated[int, Field(ge=0)]
    max_attempts: Annotated[int, Field(ge=1, le=10)]
    lease: Lease | None = None
    last_lease_id: uuid.UUID | None = None
    progress: ProgressNote | None = None
    output: NonEmptyText | None = None
    failure: FailureReason | None = None
    created_at: AwareDatetime
    updated_at: AwareDatetime

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        leased = self.status is TaskStatus.LEASED
        if leased != (self.lease is not None):
            raise ValueError("a task has a lease exactly when it is leased")
        if self.attempt > self.max_attempts:
            raise ValueError("attempt exceeds max_attempts")
        if (self.status is TaskStatus.SUCCEEDED) != (self.output is not None):
            raise ValueError("a task has output exactly when it succeeded")
        if self.status is TaskStatus.FAILED and self.failure is None:
            raise ValueError("a failed task records its failure")
        if self.lease is not None and self.lease.id != self.last_lease_id:
            raise ValueError("the active lease must be the last issued lease")
        return self

    @classmethod
    def enqueue(
        cls,
        *,
        task_id: uuid.UUID,
        session: Session,
        source_event_id: str,
        text: str,
        max_attempts: int,
        now: datetime,
        execution: Execution = Execution.SHARED,
    ) -> Self:
        return cls(
            id=task_id,
            session_id=session.id,
            source_event_id=source_event_id,
            agent=session.agent,
            engine=session.engine,
            execution=execution,
            input=text,
            status=TaskStatus.QUEUED,
            attempt=0,
            max_attempts=max_attempts,
            created_at=now,
            updated_at=now,
        )

    @property
    def terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES

    def grant_lease(self, *, lease_id: uuid.UUID, now: datetime, duration: timedelta) -> Self:
        self._require(TaskStatus.QUEUED, "lease")
        return self._evolve(
            status=TaskStatus.LEASED,
            attempt=self.attempt + 1,
            lease=Lease(id=lease_id, expires_at=now + duration),
            last_lease_id=lease_id,
            progress=None,
            updated_at=now,
        )

    def extend_lease(
        self,
        *,
        lease_id: uuid.UUID,
        now: datetime,
        duration: timedelta,
        progress: str | None,
    ) -> Self:
        lease = self._active_lease(lease_id, now)
        return self._evolve(
            lease=lease.model_copy(update={"expires_at": now + duration}),
            progress=progress if progress is not None else self.progress,
            updated_at=now,
        )

    def verify_lease(self, lease_id: uuid.UUID, now: datetime) -> None:
        """Raise ``LeaseLostError`` unless ``lease_id`` is the active, unexpired lease."""
        self._active_lease(lease_id, now)

    def already_reported(self, lease_id: uuid.UUID) -> bool:
        """True when the result for ``lease_id`` was applied before (idempotent retry)."""
        return (
            self.status not in (TaskStatus.LEASED, TaskStatus.CANCELLED)
            and self.last_lease_id == lease_id
        )

    def succeed(self, *, lease_id: uuid.UUID, text: str, now: datetime) -> Self:
        self._active_lease(lease_id, now)
        return self._evolve(status=TaskStatus.SUCCEEDED, lease=None, output=text, updated_at=now)

    def fail(self, *, lease_id: uuid.UUID, reason: str, retryable: bool, now: datetime) -> Self:
        self._active_lease(lease_id, now)
        if retryable and self.attempt < self.max_attempts:
            return self._evolve(
                status=TaskStatus.QUEUED, lease=None, failure=reason, updated_at=now
            )
        return self._evolve(status=TaskStatus.FAILED, lease=None, failure=reason, updated_at=now)

    def expire(self, now: datetime) -> Self:
        self._require(TaskStatus.LEASED, "expire")
        if self.lease is None or not self.lease.expired(now):
            raise InvalidTransitionError(f"task {self.id}: lease has not expired")
        if self.attempt < self.max_attempts:
            return self._evolve(status=TaskStatus.QUEUED, lease=None, updated_at=now)
        return self._evolve(
            status=TaskStatus.FAILED,
            lease=None,
            failure=f"lease expired on the last of {self.max_attempts} attempts",
            updated_at=now,
        )

    def abandon(self, *, reason: str, now: datetime) -> Self:
        """Fail a queued task that no worker could be started for."""
        self._require(TaskStatus.QUEUED, "abandon")
        return self._evolve(status=TaskStatus.FAILED, failure=reason, updated_at=now)

    def cancel(self, now: datetime) -> Self:
        if self.terminal:
            raise InvalidTransitionError(f"task {self.id}: cannot cancel a {self.status} task")
        return self._evolve(status=TaskStatus.CANCELLED, lease=None, updated_at=now)

    def _require(self, status: TaskStatus, action: str) -> None:
        if self.status is not status:
            raise InvalidTransitionError(f"task {self.id}: cannot {action} a {self.status} task")

    def _active_lease(self, lease_id: uuid.UUID, now: datetime) -> Lease:
        if self.lease is None or self.lease.id != lease_id:
            raise LeaseLostError(f"task {self.id}: lease {lease_id} is not active")
        if self.lease.expired(now):
            raise LeaseLostError(f"task {self.id}: lease {lease_id} has expired")
        return self.lease


class WorkerScope(FrozenModel):
    """Which tasks a worker may lease and report on.

    Shared runtimes see the tasks of their engine; isolated Jobs see isolated tasks
    of any engine. Neither ever sees the other's tasks (ADR-0021).
    """

    execution: Execution
    engine: Engine | None = None

    @model_validator(mode="after")
    def _shared_needs_engine(self) -> Self:
        if (self.execution is Execution.SHARED) != (self.engine is not None):
            raise ValueError("a shared worker serves exactly one engine")
        return self

    @classmethod
    def shared(cls, engine: Engine) -> Self:
        return cls(execution=Execution.SHARED, engine=engine)

    @classmethod
    def isolated(cls) -> Self:
        return cls(execution=Execution.ISOLATED)

    def admits(self, task: Task) -> bool:
        if task.execution is not self.execution:
            return False
        return self.engine is None or task.engine is self.engine


class Dispatch(FrozenModel):
    """One Job started for an isolated task (ADR-0021). Counts toward the daily limit."""

    id: uuid.UUID
    task_id: uuid.UUID
    job_name: Annotated[str, Field(min_length=1, max_length=63)]
    created_at: AwareDatetime


class ApprovalState(StrEnum):
    CHECKS_FAILED = "checks-failed"
    """CI failed on this commit; the owner was told, nothing to approve."""
    REQUESTED = "requested"
    MERGED = "merged"
    REJECTED = "rejected"
    SUPERSEDED = "superseded"
    BLOCKED = "blocked"
    CLOSED = "closed"


class Approval(Entity):
    """The owner's approval of one pull request commit (ADR-0020, I-33).

    One record per repository, PR and head commit: approving binds to that commit only.
    """

    id: uuid.UUID
    repository: Annotated[str, Field(min_length=3, max_length=200)]
    number: Annotated[int, Field(ge=1)]
    head_sha: Annotated[str, Field(pattern=r"^[0-9a-f]{40}$")]
    branch: Annotated[str, Field(min_length=1, max_length=255)]
    title: Annotated[str, Field(min_length=1, max_length=300)]
    url: Annotated[str, Field(min_length=1, max_length=500)]
    state: ApprovalState
    created_at: AwareDatetime
    decided_at: AwareDatetime | None = None
    decided_by: Identifier | None = None

    def resolve(self, state: ApprovalState, now: datetime, by: str | None = None) -> Self:
        if self.state is not ApprovalState.REQUESTED:
            raise InvalidTransitionError(f"approval {self.id} is already {self.state}")
        return self._evolve(state=state, decided_at=now, decided_by=by)


class NoticeStatus(StrEnum):
    PENDING = "pending"
    DELIVERED = "delivered"


class Notice(Entity):
    """A message for the owner's DM (ADR-0020). ``body`` is an ``OwnerNotice`` body."""

    id: uuid.UUID
    body: OwnerNoticeBody
    status: NoticeStatus = NoticeStatus.PENDING
    created_at: AwareDatetime

    def mark_delivered(self) -> Self:
        if self.status is NoticeStatus.DELIVERED:
            return self
        return self._evolve(status=NoticeStatus.DELIVERED)


class ReviewMessageRef(FrozenModel):
    """Where a Slack message shows a pull request: its approval message (ADR-0020 §4,
    ADR-0036) or its status card in the asking thread."""

    repository: str
    number: int
    channel: str
    ts: str


class OutboxStatus(StrEnum):
    PENDING = "pending"
    DELIVERED = "delivered"


class OutboxFile(FrozenModel):
    """The bytes of a ``FILE`` outbox message (ADR-0042)."""

    message_id: uuid.UUID
    filename: Annotated[str, StringConstraints(min_length=1, max_length=200)]
    content: Annotated[bytes, Field(min_length=1, max_length=MAX_FILE_BYTES)]


class OutboxMessage(Entity):
    """A message waiting for its connector to publish it."""

    id: uuid.UUID
    task_id: uuid.UUID
    source_event_id: Identifier
    conversation: ConversationRef
    kind: OutboundKind
    text: NonEmptyText
    status: OutboxStatus = OutboxStatus.PENDING
    created_at: AwareDatetime
    delivered_at: AwareDatetime | None = None
    not_before: AwareDatetime | None = None
    """A scheduled message is not handed to the connector before this (ADR-0042)."""

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if (self.status is OutboxStatus.DELIVERED) != (self.delivered_at is not None):
            raise ValueError("a message has delivered_at exactly when it was delivered")
        return self

    def mark_delivered(self, now: datetime) -> Self:
        if self.status is OutboxStatus.DELIVERED:
            return self
        return self._evolve(status=OutboxStatus.DELIVERED, delivered_at=now)
