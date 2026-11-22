"""ORM table models. Used only inside this package (P-14)."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import JsonValue
from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    type_annotation_map = {datetime: DateTime(timezone=True)}  # noqa: RUF012


class SessionRow(Base):
    __tablename__ = "sessions"
    __table_args__ = (
        UniqueConstraint("connector", "channel", "thread", name="uq_sessions_conversation"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    connector: Mapped[str] = mapped_column(String(32))
    channel: Mapped[str] = mapped_column(String(255))
    thread: Mapped[str] = mapped_column(String(255))
    agent: Mapped[str] = mapped_column(String(63))
    engine: Mapped[str] = mapped_column(String(16))
    engine_session_id: Mapped[str | None] = mapped_column(String(255))
    reply_channel: Mapped[str | None] = mapped_column(String(255))
    reply_thread: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime]
    updated_at: Mapped[datetime]


class TaskRow(Base):
    __tablename__ = "tasks"
    __table_args__ = (
        Index("ix_tasks_queue", "engine", "status", "created_at"),
        Index("ix_tasks_session_status", "session_id", "status"),
        Index("ix_tasks_lease_expiry", "status", "lease_expires_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    session_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("sessions.id"))
    source_event_id: Mapped[str] = mapped_column(String(255), unique=True)
    agent: Mapped[str] = mapped_column(String(63))
    engine: Mapped[str] = mapped_column(String(16))
    execution: Mapped[str] = mapped_column(String(16))
    input: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16))
    attempt: Mapped[int] = mapped_column(Integer)
    max_attempts: Mapped[int] = mapped_column(Integer)
    lease_id: Mapped[uuid.UUID | None]
    lease_expires_at: Mapped[datetime | None]
    last_lease_id: Mapped[uuid.UUID | None]
    progress: Mapped[str | None] = mapped_column(String(500))
    output: Mapped[str | None] = mapped_column(Text)
    failure: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime]
    updated_at: Mapped[datetime]


class DispatchRow(Base):
    """A Job started for an isolated task (ADR-0021)."""

    __tablename__ = "dispatches"
    __table_args__ = (
        Index("ix_dispatches_created", "created_at"),
        Index("ix_dispatches_task", "task_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    task_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tasks.id"))
    job_name: Mapped[str] = mapped_column(String(63))
    created_at: Mapped[datetime]


class ApprovalRow(Base):
    """The owner's approval of one pull request commit (ADR-0020)."""

    __tablename__ = "approvals"
    __table_args__ = (
        UniqueConstraint("repository", "number", "head_sha", name="uq_approvals_commit"),
        Index("ix_approvals_state", "state", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    repository: Mapped[str] = mapped_column(String(200))
    number: Mapped[int] = mapped_column(Integer)
    head_sha: Mapped[str] = mapped_column(String(40))
    branch: Mapped[str] = mapped_column(String(255))
    title: Mapped[str] = mapped_column(String(300))
    url: Mapped[str] = mapped_column(String(500))
    state: Mapped[str] = mapped_column(String(16))
    created_at: Mapped[datetime]
    decided_at: Mapped[datetime | None]
    decided_by: Mapped[str | None] = mapped_column(String(255))


class NoticeRow(Base):
    """A message for the owner's DM. ``body`` is an ``OwnerNotice`` body as JSON."""

    __tablename__ = "notices"
    __table_args__ = (Index("ix_notices_pending", "status", "created_at"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    body: Mapped[dict[str, JsonValue]] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(String(16))
    created_at: Mapped[datetime]


class DeploymentWatchRow(Base):
    """The deployment watcher's memory (ADR-0023). One row, id 1; ``body`` is JSON."""

    __tablename__ = "deployment_watch"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    body: Mapped[dict[str, JsonValue]] = mapped_column(JSONB)
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())


class OutboxRow(Base):
    __tablename__ = "outbox"
    __table_args__ = (Index("ix_outbox_pending", "connector", "status", "created_at"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    task_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tasks.id"))
    source_event_id: Mapped[str] = mapped_column(String(255))
    connector: Mapped[str] = mapped_column(String(32))
    channel: Mapped[str] = mapped_column(String(255))
    thread: Mapped[str] = mapped_column(String(255))
    kind: Mapped[str] = mapped_column(String(16))
    text: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16))
    created_at: Mapped[datetime]
    delivered_at: Mapped[datetime | None]


class TranscriptEntryRow(Base):
    """One engine transcript entry, stored as opaque JSON (ADR-0014)."""

    __tablename__ = "transcript_entries"
    __table_args__ = (
        Index(
            "ix_transcript_entries_key",
            "session_id",
            "project_key",
            "engine_session_id",
            "subpath",
            "id",
        ),
        Index(
            "uq_transcript_entries_uuid",
            "session_id",
            "project_key",
            "engine_session_id",
            "subpath",
            "entry_uuid",
            unique=True,
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    session_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("sessions.id"))
    project_key: Mapped[str] = mapped_column(String(255))
    engine_session_id: Mapped[str] = mapped_column(String(255))
    subpath: Mapped[str] = mapped_column(String(255))
    entry_uuid: Mapped[str | None] = mapped_column(String(255))
    entry: Mapped[dict[str, JsonValue]] = mapped_column(JSONB)
