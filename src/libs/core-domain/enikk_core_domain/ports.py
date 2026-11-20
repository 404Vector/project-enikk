"""Ports implemented by adapters (persistence-sql, k8s-auth) and by core itself."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, Self

if TYPE_CHECKING:
    import uuid
    from collections.abc import Sequence
    from datetime import datetime
    from types import TracebackType

    from enikk_contracts.agents import AgentDefinition
    from enikk_contracts.common import ConnectorKind, ConversationRef, Engine
    from enikk_core_domain.auth import ServiceIdentity
    from enikk_core_domain.model import OutboxMessage, Session, Task


class SessionRepository(Protocol):
    async def get(self, session_id: uuid.UUID) -> Session | None: ...
    async def find_by_conversation(self, conversation: ConversationRef) -> Session | None: ...
    async def add(self, session: Session) -> None: ...
    async def update(self, session: Session) -> None: ...


class TaskRepository(Protocol):
    async def get_for_update(self, task_id: uuid.UUID) -> Task | None:
        """Load a task and lock it until the unit of work ends."""
        ...

    async def find_by_source_event(self, event_id: str) -> Task | None: ...
    async def add(self, task: Task) -> None: ...
    async def update(self, task: Task) -> None: ...


class OutboxRepository(Protocol):
    async def add(self, message: OutboxMessage) -> None: ...

    async def pending(self, connector: ConnectorKind, limit: int) -> Sequence[OutboxMessage]:
        """Oldest undelivered messages for ``connector``."""
        ...

    async def get_many(
        self, connector: ConnectorKind, message_ids: Sequence[uuid.UUID]
    ) -> Sequence[OutboxMessage]:
        """Messages among ``message_ids`` that belong to ``connector``."""
        ...

    async def update(self, message: OutboxMessage) -> None: ...


class JobQueue(Protocol):
    async def claim_next(self, engine: Engine) -> Task | None:
        """Lock and return the next ``QUEUED`` task for ``engine``.

        A task is eligible only when no other task of its session is leased and
        no older task of its session is queued (I-29). Tasks locked by another
        unit of work are skipped, never waited for.
        """
        ...

    async def claim_expired(self, now: datetime, limit: int) -> Sequence[Task]:
        """Lock and return ``LEASED`` tasks whose lease expired at ``now``."""
        ...


class UnitOfWork(Protocol):
    """One transaction across repositories. Leaving without ``commit`` rolls back."""

    @property
    def sessions(self) -> SessionRepository: ...
    @property
    def tasks(self) -> TaskRepository: ...
    @property
    def outbox(self) -> OutboxRepository: ...
    @property
    def queue(self) -> JobQueue: ...

    async def commit(self) -> None: ...
    async def __aenter__(self) -> Self: ...
    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None: ...


class UnitOfWorkFactory(Protocol):
    def __call__(self) -> UnitOfWork: ...


class AgentCatalog(Protocol):
    def get(self, name: str) -> AgentDefinition | None: ...
    def names(self) -> tuple[str, ...]: ...


class Authenticator(Protocol):
    async def authenticate(self, token: str) -> ServiceIdentity:
        """Verify a bearer token. Raises ``AuthenticationError`` when it is not valid."""
        ...
