"""Unit of work: one ``AsyncSession`` (one transaction) shared by every repository."""

from __future__ import annotations

from typing import TYPE_CHECKING, Self

from sqlalchemy.exc import IntegrityError, OperationalError

from enikk_core_domain.errors import ConcurrentUpdateError, DependencyUnavailableError
from enikk_persistence_sql.repositories import (
    SqlApprovalRepository,
    SqlDeploymentWatchRepository,
    SqlDispatchRepository,
    SqlJobQueue,
    SqlNoticeRepository,
    SqlOutboxRepository,
    SqlPromptPromotionRepository,
    SqlReviewMessageRepository,
    SqlSessionRepository,
    SqlTaskRepository,
    SqlTranscriptRepository,
    SqlVaultIndexRepository,
)

if TYPE_CHECKING:
    from types import TracebackType

    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


class SqlUnitOfWork:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._db = session_factory()
        self.sessions = SqlSessionRepository(self._db)
        self.tasks = SqlTaskRepository(self._db)
        self.outbox = SqlOutboxRepository(self._db)
        self.transcripts = SqlTranscriptRepository(self._db)
        self.dispatches = SqlDispatchRepository(self._db)
        self.approvals = SqlApprovalRepository(self._db)
        self.notices = SqlNoticeRepository(self._db)
        self.deployment_watch = SqlDeploymentWatchRepository(self._db)
        self.vault = SqlVaultIndexRepository(self._db)
        self.review_messages = SqlReviewMessageRepository(self._db)
        self.prompt_promotions = SqlPromptPromotionRepository(self._db)
        self.queue = SqlJobQueue(self._db)

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self._db.close()
        if isinstance(exc, OperationalError):
            raise DependencyUnavailableError("database is unavailable") from exc

    async def commit(self) -> None:
        try:
            await self._db.commit()
        except IntegrityError as exc:
            raise ConcurrentUpdateError("transaction conflicts with existing data") from exc


class SqlUnitOfWorkFactory:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    def __call__(self) -> SqlUnitOfWork:
        return SqlUnitOfWork(self._session_factory)
