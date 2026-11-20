"""Repository and job queue implementations on one ``AsyncSession``."""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import and_, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import aliased

from enikk_contracts.tasks import TaskStatus
from enikk_core_domain.errors import ConcurrentUpdateError, NotFoundError
from enikk_core_domain.model import OutboxStatus
from enikk_persistence_sql import mappers
from enikk_persistence_sql.tables import OutboxRow, SessionRow, TaskRow

if TYPE_CHECKING:
    import uuid
    from collections.abc import Sequence
    from datetime import datetime

    from sqlalchemy.ext.asyncio import AsyncSession

    from enikk_contracts.common import ConnectorKind, ConversationRef, Engine
    from enikk_core_domain.model import OutboxMessage, Session, Task


async def _flush(db: AsyncSession, what: str) -> None:
    try:
        await db.flush()
    except IntegrityError as exc:
        raise ConcurrentUpdateError(f"{what} conflicts with existing data") from exc


class SqlSessionRepository:
    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    async def get(self, session_id: uuid.UUID) -> Session | None:
        row = await self._db.get(SessionRow, session_id)
        return mappers.session_from_row(row) if row else None

    async def find_by_conversation(self, conversation: ConversationRef) -> Session | None:
        row = await self._db.scalar(
            select(SessionRow).where(
                SessionRow.connector == conversation.connector.value,
                SessionRow.channel == conversation.channel,
                SessionRow.thread == conversation.thread,
            )
        )
        return mappers.session_from_row(row) if row else None

    async def add(self, session: Session) -> None:
        self._db.add(mappers.session_to_row(session))
        await _flush(self._db, f"session {session.id}")

    async def update(self, session: Session) -> None:
        row = await self._db.get(SessionRow, session.id)
        if row is None:
            raise NotFoundError(f"session {session.id} does not exist")
        mappers.session_to_row(session, row)
        await _flush(self._db, f"session {session.id}")


class SqlTaskRepository:
    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    async def get_for_update(self, task_id: uuid.UUID) -> Task | None:
        row = await self._db.scalar(
            select(TaskRow)
            .where(TaskRow.id == task_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        return mappers.task_from_row(row) if row else None

    async def find_by_source_event(self, event_id: str) -> Task | None:
        row = await self._db.scalar(select(TaskRow).where(TaskRow.source_event_id == event_id))
        return mappers.task_from_row(row) if row else None

    async def add(self, task: Task) -> None:
        self._db.add(mappers.task_to_row(task))
        await _flush(self._db, f"task {task.id}")

    async def update(self, task: Task) -> None:
        row = await self._db.get(TaskRow, task.id)
        if row is None:
            raise NotFoundError(f"task {task.id} does not exist")
        mappers.task_to_row(task, row)
        await _flush(self._db, f"task {task.id}")


class SqlOutboxRepository:
    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    async def add(self, message: OutboxMessage) -> None:
        self._db.add(mappers.outbox_to_row(message))
        await _flush(self._db, f"outbox message {message.id}")

    async def pending(self, connector: ConnectorKind, limit: int) -> Sequence[OutboxMessage]:
        rows = await self._db.scalars(
            select(OutboxRow)
            .where(
                OutboxRow.connector == connector.value,
                OutboxRow.status == OutboxStatus.PENDING.value,
            )
            .order_by(OutboxRow.created_at, OutboxRow.id)
            .limit(limit)
        )
        return [mappers.outbox_from_row(row) for row in rows]

    async def get_many(
        self, connector: ConnectorKind, message_ids: Sequence[uuid.UUID]
    ) -> Sequence[OutboxMessage]:
        rows = await self._db.scalars(
            select(OutboxRow).where(
                OutboxRow.connector == connector.value, OutboxRow.id.in_(list(message_ids))
            )
        )
        return [mappers.outbox_from_row(row) for row in rows]

    async def update(self, message: OutboxMessage) -> None:
        row = await self._db.get(OutboxRow, message.id)
        if row is None:
            raise NotFoundError(f"outbox message {message.id} does not exist")
        mappers.outbox_to_row(message, row)
        await _flush(self._db, f"outbox message {message.id}")


class SqlJobQueue:
    """Postgres queue over the task table (ADR-0012 §3)."""

    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    async def claim_next(self, engine: Engine) -> Task | None:
        other = aliased(TaskRow)
        blocking = (
            select(other.id)
            .where(
                other.session_id == TaskRow.session_id,
                or_(
                    other.status == TaskStatus.LEASED.value,
                    and_(
                        other.status == TaskStatus.QUEUED.value,
                        or_(
                            other.created_at < TaskRow.created_at,
                            and_(other.created_at == TaskRow.created_at, other.id < TaskRow.id),
                        ),
                    ),
                ),
            )
            .exists()
        )
        row = await self._db.scalar(
            select(TaskRow)
            .join(SessionRow, SessionRow.id == TaskRow.session_id)
            .where(
                TaskRow.engine == engine.value,
                TaskRow.status == TaskStatus.QUEUED.value,
                ~blocking,
            )
            .order_by(TaskRow.created_at, TaskRow.id)
            .limit(1)
            .with_for_update(skip_locked=True, of=[TaskRow, SessionRow])
            .execution_options(populate_existing=True)
        )
        return mappers.task_from_row(row) if row else None

    async def claim_expired(self, now: datetime, limit: int) -> Sequence[Task]:
        rows = await self._db.scalars(
            select(TaskRow)
            .where(
                TaskRow.status == TaskStatus.LEASED.value,
                TaskRow.lease_expires_at <= now,
            )
            .order_by(TaskRow.lease_expires_at, TaskRow.id)
            .limit(limit)
            .with_for_update(skip_locked=True)
            .execution_options(populate_existing=True)
        )
        return [mappers.task_from_row(row) for row in rows]
