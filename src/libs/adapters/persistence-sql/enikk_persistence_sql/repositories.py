"""Repository and job queue implementations on one ``AsyncSession``."""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import and_, delete, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import aliased

from enikk_contracts.agents import Execution
from enikk_contracts.tasks import TaskStatus
from enikk_core_domain.errors import ConcurrentUpdateError, NotFoundError
from enikk_core_domain.model import (
    ApprovalState,
    NoticeStatus,
    OutboxStatus,
    ReviewMessageRef,
)
from enikk_core_domain.transcripts import MAIN_SUBPATH, entry_uuid, new_entries
from enikk_persistence_sql import mappers
from enikk_persistence_sql.tables import (
    ApprovalRow,
    DeploymentWatchRow,
    DispatchRow,
    NoticeRow,
    OutboxRow,
    PromptPromotionRow,
    ReviewMessageRow,
    SessionRow,
    TaskRow,
    TranscriptEntryRow,
    VaultNoteRow,
)

if TYPE_CHECKING:
    import uuid
    from collections.abc import Sequence
    from datetime import datetime

    from sqlalchemy import ColumnElement
    from sqlalchemy.ext.asyncio import AsyncSession

    from enikk_contracts.common import ConnectorKind, ConversationRef, Engine
    from enikk_contracts.transcripts import TranscriptEntry, TranscriptKey
    from enikk_core_domain.deployments import DeploymentWatch
    from enikk_core_domain.memory import VaultNote
    from enikk_core_domain.model import (
        Approval,
        Dispatch,
        Notice,
        OutboxMessage,
        Session,
        Task,
    )
    from enikk_core_domain.prompts import PromptPromotion


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

    async def recent(
        self,
        since: datetime,
        limit: int,
        status: TaskStatus | None = None,
        agent: str | None = None,
    ) -> Sequence[Task]:
        query = select(TaskRow).where(TaskRow.created_at >= since)
        if status is not None:
            query = query.where(TaskRow.status == status.value)
        if agent is not None:
            query = query.where(TaskRow.agent == agent)
        rows = await self._db.scalars(
            query.order_by(TaskRow.created_at.desc(), TaskRow.id.desc()).limit(limit)
        )
        return [mappers.task_from_row(row) for row in rows]

    async def latest_in_session(self, session_id: uuid.UUID) -> Task | None:
        row = await self._db.scalar(
            select(TaskRow)
            .where(TaskRow.session_id == session_id)
            .order_by(TaskRow.created_at.desc(), TaskRow.id.desc())
            .limit(1)
        )
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

    async def posted_by_others(
        self, conversation: ConversationRef, session_id: uuid.UUID, since: datetime, limit: int
    ) -> Sequence[OutboxMessage]:
        rows = await self._db.scalars(
            select(OutboxRow)
            .join(TaskRow, TaskRow.id == OutboxRow.task_id)
            .where(
                OutboxRow.connector == conversation.connector.value,
                OutboxRow.channel == conversation.channel,
                OutboxRow.thread == conversation.thread,
                OutboxRow.created_at > since,
                TaskRow.session_id != session_id,
            )
            .order_by(OutboxRow.created_at.desc(), OutboxRow.id.desc())
            .limit(limit)
        )
        return [mappers.outbox_from_row(row) for row in reversed(list(rows))]

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


class SqlTranscriptRepository:
    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    async def append(
        self, session_id: uuid.UUID, key: TranscriptKey, entries: Sequence[TranscriptEntry]
    ) -> int:
        subpath = key.subpath or MAIN_SUBPATH
        scope = _transcript(session_id, key.project_key, key.engine_session_id, subpath)
        markers = [m for e in entries if (m := entry_uuid(e)) is not None]
        stored: list[str] = []
        if markers:
            found = await self._db.scalars(
                select(TranscriptEntryRow.entry_uuid).where(
                    *scope, TranscriptEntryRow.entry_uuid.in_(markers)
                )
            )
            stored = [m for m in found if m is not None]
        added = new_entries(entries, stored)
        self._db.add_all(
            TranscriptEntryRow(
                session_id=session_id,
                project_key=key.project_key,
                engine_session_id=key.engine_session_id,
                subpath=subpath,
                entry_uuid=entry_uuid(entry),
                entry=entry,
            )
            for entry in added
        )
        await _flush(self._db, f"transcript of session {session_id}")
        return len(added)

    async def load(self, session_id: uuid.UUID, key: TranscriptKey) -> Sequence[TranscriptEntry]:
        subpath = key.subpath or MAIN_SUBPATH
        rows = await self._db.scalars(
            select(TranscriptEntryRow.entry)
            .where(*_transcript(session_id, key.project_key, key.engine_session_id, subpath))
            .order_by(TranscriptEntryRow.id)
        )
        return list(rows)

    async def subpaths(
        self, session_id: uuid.UUID, project_key: str, engine_session_id: str
    ) -> Sequence[str]:
        rows = await self._db.scalars(
            select(TranscriptEntryRow.subpath)
            .where(
                TranscriptEntryRow.session_id == session_id,
                TranscriptEntryRow.project_key == project_key,
                TranscriptEntryRow.engine_session_id == engine_session_id,
                TranscriptEntryRow.subpath != MAIN_SUBPATH,
            )
            .distinct()
            .order_by(TranscriptEntryRow.subpath)
        )
        return list(rows)


def _transcript(
    session_id: uuid.UUID, project_key: str, engine_session_id: str, subpath: str
) -> tuple[ColumnElement[bool], ...]:
    return (
        TranscriptEntryRow.session_id == session_id,
        TranscriptEntryRow.project_key == project_key,
        TranscriptEntryRow.engine_session_id == engine_session_id,
        TranscriptEntryRow.subpath == subpath,
    )


class SqlApprovalRepository:
    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    async def add(self, approval: Approval) -> None:
        self._db.add(mappers.approval_to_row(approval))
        await _flush(self._db, f"approval {approval.id}")

    async def update(self, approval: Approval) -> None:
        row = await self._db.get(ApprovalRow, approval.id)
        if row is None:
            raise NotFoundError(f"approval {approval.id} does not exist")
        mappers.approval_to_row(approval, row)
        await _flush(self._db, f"approval {approval.id}")

    async def get_for_update(self, approval_id: uuid.UUID) -> Approval | None:
        row = await self._db.scalar(
            select(ApprovalRow)
            .where(ApprovalRow.id == approval_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        return mappers.approval_from_row(row) if row else None

    async def find(self, repository: str, number: int, head_sha: str) -> Approval | None:
        row = await self._db.scalar(
            select(ApprovalRow).where(
                ApprovalRow.repository == repository,
                ApprovalRow.number == number,
                ApprovalRow.head_sha == head_sha,
            )
        )
        return mappers.approval_from_row(row) if row else None

    async def in_state(self, state: ApprovalState) -> Sequence[Approval]:
        rows = await self._db.scalars(
            select(ApprovalRow)
            .where(ApprovalRow.state == state.value)
            .order_by(ApprovalRow.created_at, ApprovalRow.id)
        )
        return [mappers.approval_from_row(row) for row in rows]


class SqlNoticeRepository:
    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    async def add(self, notice: Notice) -> None:
        self._db.add(mappers.notice_to_row(notice))
        await _flush(self._db, f"notice {notice.id}")

    async def pending(self, limit: int) -> Sequence[Notice]:
        rows = await self._db.scalars(
            select(NoticeRow)
            .where(NoticeRow.status == NoticeStatus.PENDING.value)
            .order_by(NoticeRow.created_at, NoticeRow.id)
            .limit(limit)
        )
        return [mappers.notice_from_row(row) for row in rows]

    async def get_many(self, notice_ids: Sequence[uuid.UUID]) -> Sequence[Notice]:
        rows = await self._db.scalars(select(NoticeRow).where(NoticeRow.id.in_(list(notice_ids))))
        return [mappers.notice_from_row(row) for row in rows]

    async def recent_of_kinds(self, kinds: Sequence[str], limit: int) -> Sequence[Notice]:
        rows = await self._db.scalars(
            select(NoticeRow)
            .where(NoticeRow.body["kind"].astext.in_(list(kinds)))
            .order_by(NoticeRow.created_at.desc(), NoticeRow.id.desc())
            .limit(limit)
        )
        return [mappers.notice_from_row(row) for row in rows]

    async def update(self, notice: Notice) -> None:
        row = await self._db.get(NoticeRow, notice.id)
        if row is None:
            raise NotFoundError(f"notice {notice.id} does not exist")
        mappers.notice_to_row(notice, row)
        await _flush(self._db, f"notice {notice.id}")


class SqlDeploymentWatchRepository:
    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    async def load_for_update(self) -> DeploymentWatch | None:
        row = await self._db.scalar(
            select(DeploymentWatchRow)
            .where(DeploymentWatchRow.id == mappers.WATCH_ROW_ID)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        return mappers.deployment_watch_from_row(row) if row else None

    async def save(self, watch: DeploymentWatch) -> None:
        row = await self._db.get(DeploymentWatchRow, mappers.WATCH_ROW_ID)
        if row is None:
            self._db.add(mappers.deployment_watch_to_row(watch))
        else:
            mappers.deployment_watch_to_row(watch, row)
        await _flush(self._db, "deployment watch")


class SqlReviewMessageRepository:
    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    async def get(self, repository: str, number: int) -> ReviewMessageRef | None:
        row = await self._db.get(ReviewMessageRow, (repository, number))
        if row is None:
            return None
        return ReviewMessageRef(
            repository=row.repository, number=row.number, channel=row.channel, ts=row.ts
        )

    async def put(self, ref: ReviewMessageRef) -> None:
        row = await self._db.get(ReviewMessageRow, (ref.repository, ref.number))
        if row is None:
            row = ReviewMessageRow(repository=ref.repository, number=ref.number)
            self._db.add(row)
        row.channel = ref.channel
        row.ts = ref.ts
        await _flush(self._db, f"review message of {ref.repository}#{ref.number}")


class SqlPromptPromotionRepository:
    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    async def add(self, promotion: PromptPromotion) -> None:
        self._db.add(mappers.prompt_promotion_to_row(promotion))
        await _flush(self._db, f"prompt promotion {promotion.id}")

    async def update(self, promotion: PromptPromotion) -> None:
        row = await self._db.get(PromptPromotionRow, promotion.id)
        if row is None:
            raise NotFoundError(f"prompt promotion {promotion.id} does not exist")
        mappers.prompt_promotion_to_row(promotion, row)
        await _flush(self._db, f"prompt promotion {promotion.id}")

    async def get_for_update(self, promotion_id: uuid.UUID) -> PromptPromotion | None:
        row = await self._db.scalar(
            select(PromptPromotionRow)
            .where(PromptPromotionRow.id == promotion_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        return mappers.prompt_promotion_from_row(row) if row else None


class SqlVaultIndexRepository:
    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    async def shas(self) -> dict[str, str]:
        rows = await self._db.execute(select(VaultNoteRow.path, VaultNoteRow.blob_sha))
        return {path: sha for path, sha in rows}

    async def upsert(self, note: VaultNote) -> None:
        row = await self._db.get(VaultNoteRow, note.path)
        if row is None:
            self._db.add(mappers.vault_note_to_row(note))
        else:
            mappers.vault_note_to_row(note, row)
        await _flush(self._db, f"vault note {note.path}")

    async def remove(self, paths: Sequence[str]) -> None:
        if paths:
            await self._db.execute(delete(VaultNoteRow).where(VaultNoteRow.path.in_(list(paths))))

    async def under(self, prefix: str) -> Sequence[VaultNote]:
        rows = await self._db.scalars(
            select(VaultNoteRow)
            .where(VaultNoteRow.path.startswith(prefix, autoescape=True))
            .order_by(VaultNoteRow.path)
        )
        return [mappers.vault_note_from_row(row) for row in rows]

    async def search(self, words: Sequence[str], limit: int) -> Sequence[VaultNote]:
        query = select(VaultNoteRow)
        for word in words:
            pattern = f"%{_escape_like(word)}%"
            query = query.where(
                or_(
                    VaultNoteRow.title.ilike(pattern, escape="\\"),
                    VaultNoteRow.body.ilike(pattern, escape="\\"),
                )
            )
        rows = await self._db.scalars(query.order_by(VaultNoteRow.path).limit(limit))
        return [mappers.vault_note_from_row(row) for row in rows]


def _escape_like(word: str) -> str:
    return word.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


class SqlDispatchRepository:
    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    async def add(self, dispatch: Dispatch) -> None:
        self._db.add(mappers.dispatch_to_row(dispatch))
        await _flush(self._db, f"dispatch {dispatch.id}")

    async def count_since(self, since: datetime) -> int:
        count = await self._db.scalar(
            select(func.count()).select_from(DispatchRow).where(DispatchRow.created_at >= since)
        )
        return count or 0

    async def count_for(self, task_id: uuid.UUID) -> int:
        count = await self._db.scalar(
            select(func.count()).select_from(DispatchRow).where(DispatchRow.task_id == task_id)
        )
        return count or 0


def _session_free() -> ColumnElement[bool]:
    """No other task of the same session is leased or queued ahead (I-29)."""
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
    return ~blocking


class SqlJobQueue:
    """Postgres queue over the task table (ADR-0012 §3)."""

    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    async def claim_next(self, engine: Engine) -> Task | None:
        row = await self._db.scalar(
            select(TaskRow)
            .join(SessionRow, SessionRow.id == TaskRow.session_id)
            .where(
                TaskRow.engine == engine.value,
                TaskRow.execution == Execution.SHARED.value,
                TaskRow.status == TaskStatus.QUEUED.value,
                _session_free(),
            )
            .order_by(TaskRow.created_at, TaskRow.id)
            .limit(1)
            .with_for_update(skip_locked=True, of=[TaskRow, SessionRow])
            .execution_options(populate_existing=True)
        )
        return mappers.task_from_row(row) if row else None

    async def next_isolated(self) -> Task | None:
        row = await self._db.scalar(
            select(TaskRow)
            .where(
                TaskRow.execution == Execution.ISOLATED.value,
                TaskRow.status == TaskStatus.QUEUED.value,
                _session_free(),
            )
            .order_by(TaskRow.created_at, TaskRow.id)
            .limit(1)
        )
        return mappers.task_from_row(row) if row else None

    async def claim_isolated(self, task_id: uuid.UUID) -> Task | None:
        row = await self._db.scalar(
            select(TaskRow)
            .join(SessionRow, SessionRow.id == TaskRow.session_id)
            .where(
                TaskRow.id == task_id,
                TaskRow.execution == Execution.ISOLATED.value,
                TaskRow.status == TaskStatus.QUEUED.value,
                _session_free(),
            )
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
