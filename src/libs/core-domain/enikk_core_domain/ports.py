"""Ports implemented by adapters (persistence-sql, k8s-auth) and by core itself."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, Self

if TYPE_CHECKING:
    import uuid
    from collections.abc import Sequence
    from datetime import datetime
    from types import TracebackType

    from pydantic import SecretStr

    from enikk_contracts.agents import AgentDefinition
    from enikk_contracts.common import ConnectorKind, ConversationRef, Engine
    from enikk_contracts.credentials import CredentialName
    from enikk_contracts.tasks import TaskStatus
    from enikk_contracts.transcripts import TranscriptEntry, TranscriptKey
    from enikk_core_domain.auth import ServiceIdentity
    from enikk_core_domain.deployments import AppObservation, DeploymentWatch
    from enikk_core_domain.memory import VaultNote
    from enikk_core_domain.model import (
        Approval,
        ApprovalState,
        Dispatch,
        Notice,
        OutboxMessage,
        ReviewMessageRef,
        Session,
        Task,
    )
    from enikk_core_domain.prompts import PromptPromotion


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
    async def recent(
        self,
        since: datetime,
        limit: int,
        status: TaskStatus | None = None,
        agent: str | None = None,
    ) -> Sequence[Task]:
        """Tasks created since ``since``, newest first (read-only, ADR-0029)."""
        ...

    async def latest_in_session(self, session_id: uuid.UUID) -> Task | None:
        """The most recently created task of the session, without locking it."""
        ...

    async def add(self, task: Task) -> None: ...
    async def update(self, task: Task) -> None: ...


class OutboxRepository(Protocol):
    async def add(self, message: OutboxMessage) -> None: ...

    async def posted_by_others(
        self, conversation: ConversationRef, session_id: uuid.UUID, since: datetime, limit: int
    ) -> Sequence[OutboxMessage]:
        """Messages for ``conversation`` created after ``since`` by tasks of other
        sessions (delegates, core's notes), oldest first, at most the newest ``limit``.
        """
        ...

    async def pending(self, connector: ConnectorKind, limit: int) -> Sequence[OutboxMessage]:
        """Oldest undelivered messages for ``connector``."""
        ...

    async def get_many(
        self, connector: ConnectorKind, message_ids: Sequence[uuid.UUID]
    ) -> Sequence[OutboxMessage]:
        """Messages among ``message_ids`` that belong to ``connector``."""
        ...

    async def update(self, message: OutboxMessage) -> None: ...


class TranscriptRepository(Protocol):
    """Engine transcripts, scoped to the session that owns them (ADR-0014)."""

    async def append(
        self, session_id: uuid.UUID, key: TranscriptKey, entries: Sequence[TranscriptEntry]
    ) -> int:
        """Append in order and return how many were stored.

        Entries whose ``uuid`` is already stored under ``key`` are skipped.
        """
        ...

    async def load(self, session_id: uuid.UUID, key: TranscriptKey) -> Sequence[TranscriptEntry]:
        """Entries in append order; empty when nothing was stored."""
        ...

    async def subpaths(
        self, session_id: uuid.UUID, project_key: str, engine_session_id: str
    ) -> Sequence[str]:
        """Distinct non-empty subpaths stored under the main transcript, sorted."""
        ...


class JobQueue(Protocol):
    async def claim_next(self, engine: Engine, held: uuid.UUID | None = None) -> Task | None:
        """Lock and return the next ``QUEUED`` shared task for ``engine``.

        Held tasks (event ID starting with ``HELD_EVENT_PREFIX``) are skipped. With
        ``held``, only that task is returned, and only if it is a held task.

        A task is eligible only when no other task of its session is leased and
        no older task of its session is queued (I-29). Tasks locked by another
        unit of work are skipped, never waited for. Isolated tasks are never
        returned (ADR-0021).
        """
        ...

    async def next_isolated(self) -> Task | None:
        """The oldest eligible ``QUEUED`` isolated task, without locking it."""
        ...

    async def claim_isolated(self, task_id: uuid.UUID) -> Task | None:
        """Lock and return ``task_id`` if it is an eligible ``QUEUED`` isolated task."""
        ...

    async def claim_expired(self, now: datetime, limit: int) -> Sequence[Task]:
        """Lock and return ``LEASED`` tasks whose lease expired at ``now``."""
        ...


class DispatchRepository(Protocol):
    """Jobs started for isolated tasks (ADR-0021)."""

    async def add(self, dispatch: Dispatch) -> None: ...
    async def count_since(self, since: datetime) -> int: ...
    async def count_for(self, task_id: uuid.UUID) -> int: ...


class ApprovalRepository(Protocol):
    """Owner approvals of pull request commits (ADR-0020)."""

    async def add(self, approval: Approval) -> None: ...
    async def update(self, approval: Approval) -> None: ...

    async def get_for_update(self, approval_id: uuid.UUID) -> Approval | None:
        """Load an approval and lock it until the unit of work ends."""
        ...

    async def find(self, repository: str, number: int, head_sha: str) -> Approval | None: ...

    async def in_state(self, state: ApprovalState) -> Sequence[Approval]:
        """Every approval in ``state``, oldest first."""
        ...


class NoticeRepository(Protocol):
    """The owner's DM queue (ADR-0020)."""

    async def add(self, notice: Notice) -> None: ...

    async def pending(self, limit: int) -> Sequence[Notice]:
        """Oldest undelivered notices first."""
        ...

    async def get_many(self, notice_ids: Sequence[uuid.UUID]) -> Sequence[Notice]: ...
    async def recent_of_kinds(self, kinds: Sequence[str], limit: int) -> Sequence[Notice]:
        """The newest notices whose body has one of ``kinds``."""
        ...

    async def update(self, notice: Notice) -> None: ...


class DeploymentWatchRepository(Protocol):
    """The deployment watcher's memory: a single document (ADR-0023 §1)."""

    async def load_for_update(self) -> DeploymentWatch | None:
        """Load it and lock it until the unit of work ends; ``None`` before the first save."""
        ...

    async def save(self, watch: DeploymentWatch) -> None: ...


class VaultIndexRepository(Protocol):
    """Core's index of the owner's vault notes (ADR-0028). The vault is the source."""

    async def shas(self) -> dict[str, str]:
        """Path to blob SHA of every indexed note."""
        ...

    async def upsert(self, note: VaultNote) -> None: ...
    async def remove(self, paths: Sequence[str]) -> None: ...

    async def under(self, prefix: str) -> Sequence[VaultNote]:
        """Notes whose path starts with ``prefix``."""
        ...

    async def search(self, words: Sequence[str], limit: int) -> Sequence[VaultNote]:
        """Notes whose title or body contains every word (case-insensitive)."""
        ...


class ReviewMessageRepository(Protocol):
    async def get(self, repository: str, number: int) -> ReviewMessageRef | None: ...
    async def put(self, ref: ReviewMessageRef) -> None:
        """Create or replace the message of that pull request."""
        ...


class PromptPromotionRepository(Protocol):
    """The owner's promotion requests of prompt candidates (ADR-0032)."""

    async def add(self, promotion: PromptPromotion) -> None: ...
    async def update(self, promotion: PromptPromotion) -> None: ...

    async def get_for_update(self, promotion_id: uuid.UUID) -> PromptPromotion | None:
        """Load a promotion and lock it until the unit of work ends."""
        ...


class UnitOfWork(Protocol):
    """One transaction across repositories. Leaving without ``commit`` rolls back."""

    @property
    def approvals(self) -> ApprovalRepository: ...
    @property
    def notices(self) -> NoticeRepository: ...
    @property
    def deployment_watch(self) -> DeploymentWatchRepository: ...
    @property
    def vault(self) -> VaultIndexRepository: ...
    @property
    def review_messages(self) -> ReviewMessageRepository: ...
    @property
    def prompt_promotions(self) -> PromptPromotionRepository: ...

    @property
    def sessions(self) -> SessionRepository: ...
    @property
    def dispatches(self) -> DispatchRepository: ...
    @property
    def tasks(self) -> TaskRepository: ...
    @property
    def outbox(self) -> OutboxRepository: ...
    @property
    def transcripts(self) -> TranscriptRepository: ...
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


class CredentialStore(Protocol):
    """Credential values core hands out (ADR-0013). Read on every call, so rotation applies."""

    def get(self, name: CredentialName) -> SecretStr | None:
        """The current value, or ``None`` when it is not set."""
        ...


class JobLauncher(Protocol):
    """Starts one isolated worker Job per task (ADR-0021)."""

    async def ready(self) -> bool:
        """Whether the Job template is deployed. Without it nothing is launched."""
        ...

    async def running(self) -> int:
        """Jobs that have not finished yet."""
        ...

    async def launch(self, task_id: uuid.UUID) -> str:
        """Create the Job for ``task_id`` and return its name."""
        ...


class Authenticator(Protocol):
    async def authenticate(self, token: str) -> ServiceIdentity:
        """Verify a bearer token. Raises ``AuthenticationError`` when it is not valid."""
        ...


class ApplicationReader(Protocol):
    """Reads Argo CD Applications (ADR-0023 §1). Read-only."""

    async def observe(self, apps: Sequence[str]) -> list[AppObservation]:
        """Current state of each named app that exists."""
        ...
