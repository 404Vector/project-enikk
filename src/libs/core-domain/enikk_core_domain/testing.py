"""Test support: in-memory adapters, builders and a port conformance suite.

The conformance checks run against every ``UnitOfWork`` implementation
(in-memory here, SQL in persistence-sql), so all of them behave alike.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Self

from enikk_contracts.agents import AgentDefinition, Execution, PromptRef
from enikk_contracts.approvals import ChecksFailed, PullRequestCard
from enikk_contracts.common import ConnectorKind, ConversationRef, Engine
from enikk_contracts.outbox import OutboundKind
from enikk_contracts.tasks import TaskStatus
from enikk_contracts.transcripts import TranscriptKey
from enikk_core_domain.deployments import AppBaseline, DeploymentWatch
from enikk_core_domain.errors import AuthenticationError, ConcurrentUpdateError
from enikk_core_domain.memory import VaultNote
from enikk_core_domain.model import (
    Approval,
    ApprovalState,
    Dispatch,
    Notice,
    NoticeStatus,
    OutboxMessage,
    OutboxStatus,
    ReviewMessageRef,
    Session,
    Task,
)
from enikk_core_domain.transcripts import MAIN_SUBPATH, entry_uuid, new_entries

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Sequence
    from types import TracebackType

    from pydantic import SecretStr

    from enikk_contracts.credentials import CredentialName
    from enikk_contracts.transcripts import TranscriptEntry
    from enikk_core_domain.auth import ServiceIdentity
    from enikk_core_domain.ports import UnitOfWorkFactory

T0 = datetime(2026, 1, 1, tzinfo=UTC)

# ---------------------------------------------------------------- builders


def agent(
    name: str = "assistant",
    engine: Engine = Engine.CLAUDE,
    execution: Execution = Execution.SHARED,
) -> AgentDefinition:
    return AgentDefinition(
        name=name,
        description=f"{name} for tests",
        engine=engine,
        execution=execution,
        prompt=PromptRef(name=name, label="production"),
    )


def conversation(thread: str = "1.0", channel: str = "C1") -> ConversationRef:
    return ConversationRef(connector=ConnectorKind.SLACK, channel=channel, thread=thread)


def session(*, thread: str = "1.0", engine: Engine = Engine.CLAUDE, now: datetime = T0) -> Session:
    return Session.start(
        session_id=uuid.uuid4(),
        conversation=conversation(thread),
        agent=agent(engine=engine),
        now=now,
    )


def task(
    owner: Session,
    *,
    event: str | None = None,
    now: datetime = T0,
    execution: Execution = Execution.SHARED,
) -> Task:
    return Task.enqueue(
        task_id=uuid.uuid4(),
        session=owner,
        source_event_id=event or f"ev-{uuid.uuid4()}",
        text="hello",
        max_attempts=3,
        now=now,
        execution=execution,
    )


def outbox_message(owner: Session, related: Task, *, now: datetime = T0) -> OutboxMessage:
    return OutboxMessage(
        id=uuid.uuid4(),
        task_id=related.id,
        source_event_id=related.source_event_id,
        conversation=owner.conversation,
        kind=OutboundKind.REPLY,
        text="hi there",
        created_at=now,
    )


# ---------------------------------------------------------------- in-memory adapters


class InMemoryDatabase:
    """Committed state shared by every ``InMemoryUnitOfWork`` created from it."""

    def __init__(self) -> None:
        self.sessions: dict[uuid.UUID, Session] = {}
        self.tasks: dict[uuid.UUID, Task] = {}
        self.outbox: dict[uuid.UUID, OutboxMessage] = {}
        self.transcripts: dict[_Slot, tuple[TranscriptEntry, ...]] = {}
        self.dispatches: dict[uuid.UUID, Dispatch] = {}
        self.approvals: dict[uuid.UUID, Approval] = {}
        self.notices: dict[uuid.UUID, Notice] = {}
        self.deployment_watch: DeploymentWatch | None = None
        self.vault: dict[str, VaultNote] = {}
        self.review_messages: dict[tuple[str, int], ReviewMessageRef] = {}
        self.locks: dict[uuid.UUID, object] = {}

    def unit_of_work(self) -> InMemoryUnitOfWork:
        return InMemoryUnitOfWork(self)


class _Staged:
    def __init__(self, db: InMemoryDatabase) -> None:
        self.sessions = dict(db.sessions)
        self.tasks = dict(db.tasks)
        self.outbox = dict(db.outbox)
        self.transcripts = dict(db.transcripts)
        self.dispatches = dict(db.dispatches)
        self.approvals = dict(db.approvals)
        self.notices = dict(db.notices)
        self.deployment_watch = db.deployment_watch
        self.vault = dict(db.vault)
        self.review_messages = dict(db.review_messages)


class _ReviewMessages:
    def __init__(self, staged: _Staged) -> None:
        self._s = staged

    async def get(self, repository: str, number: int) -> ReviewMessageRef | None:
        return self._s.review_messages.get((repository, number))

    async def put(self, ref: ReviewMessageRef) -> None:
        self._s.review_messages[(ref.repository, ref.number)] = ref


class _Vault:
    def __init__(self, staged: _Staged) -> None:
        self._s = staged

    async def shas(self) -> dict[str, str]:
        return {path: note.blob_sha for path, note in self._s.vault.items()}

    async def upsert(self, note: VaultNote) -> None:
        self._s.vault[note.path] = note

    async def remove(self, paths: Sequence[str]) -> None:
        for path in paths:
            self._s.vault.pop(path, None)

    async def under(self, prefix: str) -> Sequence[VaultNote]:
        return sorted(
            (n for n in self._s.vault.values() if n.path.startswith(prefix)), key=lambda n: n.path
        )

    async def search(self, words: Sequence[str], limit: int) -> Sequence[VaultNote]:
        lowered = [w.lower() for w in words]
        found = [
            n
            for n in self._s.vault.values()
            if all(w in n.title.lower() or w in n.body.lower() for w in lowered)
        ]
        return sorted(found, key=lambda n: n.path)[:limit]


class _DeploymentWatch:
    def __init__(self, staged: _Staged) -> None:
        self._s = staged

    async def load_for_update(self) -> DeploymentWatch | None:
        return self._s.deployment_watch

    async def save(self, watch: DeploymentWatch) -> None:
        self._s.deployment_watch = watch


class _Approvals:
    def __init__(self, staged: _Staged) -> None:
        self._s = staged

    async def add(self, approval: Approval) -> None:
        self._s.approvals[approval.id] = approval

    async def update(self, approval: Approval) -> None:
        self._s.approvals[approval.id] = approval

    async def get_for_update(self, approval_id: uuid.UUID) -> Approval | None:
        return self._s.approvals.get(approval_id)

    async def find(self, repository: str, number: int, head_sha: str) -> Approval | None:
        return next(
            (
                a
                for a in self._s.approvals.values()
                if (a.repository, a.number, a.head_sha) == (repository, number, head_sha)
            ),
            None,
        )

    async def in_state(self, state: ApprovalState) -> Sequence[Approval]:
        found = [a for a in self._s.approvals.values() if a.state is state]
        return sorted(found, key=lambda a: (a.created_at, a.id))


class _Notices:
    def __init__(self, staged: _Staged) -> None:
        self._s = staged

    async def add(self, notice: Notice) -> None:
        self._s.notices[notice.id] = notice

    async def pending(self, limit: int) -> Sequence[Notice]:
        found = [n for n in self._s.notices.values() if n.status is NoticeStatus.PENDING]
        return sorted(found, key=lambda n: (n.created_at, n.id))[:limit]

    async def get_many(self, notice_ids: Sequence[uuid.UUID]) -> Sequence[Notice]:
        wanted = set(notice_ids)
        return [n for n in self._s.notices.values() if n.id in wanted]

    async def update(self, notice: Notice) -> None:
        self._s.notices[notice.id] = notice


class _Dispatches:
    def __init__(self, staged: _Staged) -> None:
        self._s = staged

    async def add(self, dispatch: Dispatch) -> None:
        self._s.dispatches[dispatch.id] = dispatch

    async def count_since(self, since: datetime) -> int:
        return sum(1 for d in self._s.dispatches.values() if d.created_at >= since)

    async def count_for(self, task_id: uuid.UUID) -> int:
        return sum(1 for d in self._s.dispatches.values() if d.task_id == task_id)


class _Sessions:
    def __init__(self, staged: _Staged) -> None:
        self._s = staged

    async def get(self, session_id: uuid.UUID) -> Session | None:
        return self._s.sessions.get(session_id)

    async def find_by_conversation(self, conversation: ConversationRef) -> Session | None:
        return next((s for s in self._s.sessions.values() if s.conversation == conversation), None)

    async def add(self, session: Session) -> None:
        self._s.sessions[session.id] = session

    async def update(self, session: Session) -> None:
        self._s.sessions[session.id] = session


class _Tasks:
    def __init__(self, staged: _Staged) -> None:
        self._s = staged

    async def get_for_update(self, task_id: uuid.UUID) -> Task | None:
        return self._s.tasks.get(task_id)

    async def find_by_source_event(self, event_id: str) -> Task | None:
        return next((t for t in self._s.tasks.values() if t.source_event_id == event_id), None)

    async def latest_in_session(self, session_id: uuid.UUID) -> Task | None:
        found = [t for t in self._s.tasks.values() if t.session_id == session_id]
        return max(found, key=lambda t: (t.created_at, t.id), default=None)

    async def add(self, task: Task) -> None:
        self._s.tasks[task.id] = task

    async def update(self, task: Task) -> None:
        self._s.tasks[task.id] = task


class _Outbox:
    def __init__(self, staged: _Staged) -> None:
        self._s = staged

    async def add(self, message: OutboxMessage) -> None:
        self._s.outbox[message.id] = message

    async def posted_by_others(
        self, conversation: ConversationRef, session_id: uuid.UUID, since: datetime, limit: int
    ) -> Sequence[OutboxMessage]:
        found = [
            m
            for m in self._s.outbox.values()
            if m.conversation == conversation
            and m.created_at > since
            and (task := self._s.tasks.get(m.task_id)) is not None
            and task.session_id != session_id
        ]
        return sorted(found, key=lambda m: (m.created_at, m.id))[-limit:]

    async def pending(self, connector: ConnectorKind, limit: int) -> Sequence[OutboxMessage]:
        found = [
            m
            for m in self._s.outbox.values()
            if m.status is OutboxStatus.PENDING and m.conversation.connector is connector
        ]
        return sorted(found, key=lambda m: (m.created_at, m.id))[:limit]

    async def get_many(
        self, connector: ConnectorKind, message_ids: Sequence[uuid.UUID]
    ) -> Sequence[OutboxMessage]:
        wanted = set(message_ids)
        return [
            m
            for m in self._s.outbox.values()
            if m.id in wanted and m.conversation.connector is connector
        ]

    async def update(self, message: OutboxMessage) -> None:
        self._s.outbox[message.id] = message


type _Slot = tuple[uuid.UUID, str, str, str]


def _slot(session_id: uuid.UUID, key: TranscriptKey) -> _Slot:
    return (session_id, key.project_key, key.engine_session_id, key.subpath or MAIN_SUBPATH)


class _Transcripts:
    def __init__(self, staged: _Staged) -> None:
        self._s = staged

    async def append(
        self, session_id: uuid.UUID, key: TranscriptKey, entries: Sequence[TranscriptEntry]
    ) -> int:
        slot = _slot(session_id, key)
        stored = self._s.transcripts.get(slot, ())
        markers = (m for e in stored if (m := entry_uuid(e)) is not None)
        added = new_entries(entries, markers)
        self._s.transcripts[slot] = (*stored, *added)
        return len(added)

    async def load(self, session_id: uuid.UUID, key: TranscriptKey) -> Sequence[TranscriptEntry]:
        return self._s.transcripts.get(_slot(session_id, key), ())

    async def subpaths(
        self, session_id: uuid.UUID, project_key: str, engine_session_id: str
    ) -> Sequence[str]:
        owner = (session_id, project_key, engine_session_id)
        return sorted({s[3] for s in self._s.transcripts if s[:3] == owner and s[3]})


class _Queue:
    def __init__(self, db: InMemoryDatabase, staged: _Staged, owner: object) -> None:
        self._db = db
        self._s = staged
        self._owner = owner

    def _locked_by_other(self, key: uuid.UUID) -> bool:
        holder = self._db.locks.get(key)
        return holder is not None and holder is not self._owner

    def _lock(self, *keys: uuid.UUID) -> None:
        for key in keys:
            self._db.locks[key] = self._owner

    def _eligible(self, candidate: Task, tasks: list[Task]) -> bool:
        if candidate.status is not TaskStatus.QUEUED:
            return False
        if self._locked_by_other(candidate.id) or self._locked_by_other(candidate.session_id):
            return False
        siblings = [t for t in tasks if t.session_id == candidate.session_id]
        if any(t.status is TaskStatus.LEASED for t in siblings):
            return False
        return not any(
            t.status is TaskStatus.QUEUED
            and (t.created_at, t.id) < (candidate.created_at, candidate.id)
            for t in siblings
        )

    def _ordered(self) -> list[Task]:
        return sorted(self._s.tasks.values(), key=lambda t: (t.created_at, t.id))

    async def claim_next(self, engine: Engine) -> Task | None:
        tasks = self._ordered()
        for candidate in tasks:
            if candidate.engine is not engine or candidate.execution is not Execution.SHARED:
                continue
            if self._eligible(candidate, tasks):
                self._lock(candidate.id, candidate.session_id)
                return candidate
        return None

    async def next_isolated(self) -> Task | None:
        tasks = self._ordered()
        return next(
            (t for t in tasks if t.execution is Execution.ISOLATED and self._eligible(t, tasks)),
            None,
        )

    async def claim_isolated(self, task_id: uuid.UUID) -> Task | None:
        tasks = self._ordered()
        candidate = self._s.tasks.get(task_id)
        if candidate is None or candidate.execution is not Execution.ISOLATED:
            return None
        if not self._eligible(candidate, tasks):
            return None
        self._lock(candidate.id, candidate.session_id)
        return candidate

    async def claim_expired(self, now: datetime, limit: int) -> Sequence[Task]:
        found = [
            t
            for t in self._s.tasks.values()
            if t.status is TaskStatus.LEASED
            and t.lease is not None
            and t.lease.expired(now)
            and not self._locked_by_other(t.id)
        ]
        found.sort(key=lambda t: (t.lease.expires_at if t.lease else now, t.id))
        claimed = found[:limit]
        self._lock(*(t.id for t in claimed))
        return claimed


class InMemoryUnitOfWork:
    def __init__(self, db: InMemoryDatabase) -> None:
        self._db = db
        self._owner = object()
        self._staged = _Staged(db)
        self.sessions = _Sessions(self._staged)
        self.tasks = _Tasks(self._staged)
        self.outbox = _Outbox(self._staged)
        self.transcripts = _Transcripts(self._staged)
        self.dispatches = _Dispatches(self._staged)
        self.approvals = _Approvals(self._staged)
        self.notices = _Notices(self._staged)
        self.deployment_watch = _DeploymentWatch(self._staged)
        self.vault = _Vault(self._staged)
        self.review_messages = _ReviewMessages(self._staged)
        self.queue = _Queue(db, self._staged, self._owner)
        self.commits = 0

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        for key in [k for k, holder in self._db.locks.items() if holder is self._owner]:
            del self._db.locks[key]

    async def commit(self) -> None:
        self._check_unique()
        self._db.sessions = dict(self._staged.sessions)
        self._db.tasks = dict(self._staged.tasks)
        self._db.outbox = dict(self._staged.outbox)
        self._db.transcripts = dict(self._staged.transcripts)
        self._db.dispatches = dict(self._staged.dispatches)
        self._db.approvals = dict(self._staged.approvals)
        self._db.notices = dict(self._staged.notices)
        self._db.deployment_watch = self._staged.deployment_watch
        self._db.vault = dict(self._staged.vault)
        self._db.review_messages = dict(self._staged.review_messages)
        self.commits += 1

    def _check_unique(self) -> None:
        sessions = {**self._db.sessions, **self._staged.sessions}.values()
        tasks = {**self._db.tasks, **self._staged.tasks}.values()
        if _has_duplicates([s.conversation for s in sessions]):
            raise ConcurrentUpdateError("a session for this conversation already exists")
        if _has_duplicates([t.source_event_id for t in tasks]):
            raise ConcurrentUpdateError("a task for this event already exists")
        approvals = {**self._db.approvals, **self._staged.approvals}.values()
        if _has_duplicates([(a.repository, a.number, a.head_sha) for a in approvals]):
            raise ConcurrentUpdateError("an approval for this commit already exists")


def _has_duplicates(values: Sequence[object]) -> bool:
    return len(set(values)) != len(values)


class StaticAgentCatalog:
    def __init__(self, *definitions: AgentDefinition) -> None:
        self._by_name = {d.name: d for d in definitions}

    def get(self, name: str) -> AgentDefinition | None:
        return self._by_name.get(name)

    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._by_name))

    def remove(self, name: str) -> None:
        del self._by_name[name]

    def put(self, definition: AgentDefinition) -> None:
        self._by_name[definition.name] = definition


class StaticCredentialStore:
    def __init__(self, values: dict[CredentialName, SecretStr]) -> None:
        self.values = values

    def get(self, name: CredentialName) -> SecretStr | None:
        return self.values.get(name)


class StaticAuthenticator:
    """Accepts exactly the tokens it was given."""

    def __init__(self, tokens: dict[str, ServiceIdentity]) -> None:
        self._tokens = tokens

    async def authenticate(self, token: str) -> ServiceIdentity:
        identity = self._tokens.get(token)
        if identity is None:
            raise AuthenticationError("token rejected")
        return identity


# ---------------------------------------------------------------- conformance suite


def _expect(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


async def check_round_trip(new_uow: UnitOfWorkFactory) -> None:
    owner = session()
    work = task(owner, event="ev-1")
    message = outbox_message(owner, work)
    async with new_uow() as uow:
        await uow.sessions.add(owner)
        await uow.tasks.add(work)
        await uow.outbox.add(message)
        await uow.commit()
    async with new_uow() as uow:
        _expect(await uow.sessions.get(owner.id) == owner, "session by id")
        found = await uow.sessions.find_by_conversation(owner.conversation)
        _expect(found == owner, "session by conversation")
        _expect(await uow.tasks.get_for_update(work.id) == work, "task by id")
        _expect(await uow.tasks.find_by_source_event("ev-1") == work, "task by event")
        _expect(list(await uow.outbox.pending(ConnectorKind.SLACK, 10)) == [message], "pending")
        many = await uow.outbox.get_many(ConnectorKind.SLACK, [message.id, uuid.uuid4()])
        _expect(list(many) == [message], "get_many")


async def check_updates_persist(new_uow: UnitOfWorkFactory) -> None:
    owner = session()
    work = task(owner)
    message = outbox_message(owner, work)
    async with new_uow() as uow:
        await uow.sessions.add(owner)
        await uow.tasks.add(work)
        await uow.outbox.add(message)
        await uow.commit()
    leased = work.grant_lease(lease_id=uuid.uuid4(), now=T0, duration=timedelta(minutes=1))
    bound = owner.bind_engine_session("engine-1", T0)
    delivered = message.mark_delivered(T0)
    async with new_uow() as uow:
        await uow.tasks.update(leased)
        await uow.sessions.update(bound)
        await uow.outbox.update(delivered)
        await uow.commit()
    async with new_uow() as uow:
        _expect(await uow.tasks.get_for_update(work.id) == leased, "task update")
        _expect(await uow.sessions.get(owner.id) == bound, "session update")
        _expect(not await uow.outbox.pending(ConnectorKind.SLACK, 10), "delivered not pending")


async def check_rollback_without_commit(new_uow: UnitOfWorkFactory) -> None:
    owner = session()
    async with new_uow() as uow:
        await uow.sessions.add(owner)
    async with new_uow() as uow:
        _expect(await uow.sessions.get(owner.id) is None, "uncommitted session must vanish")


async def check_duplicate_event_rejected(new_uow: UnitOfWorkFactory) -> None:
    owner = session()
    async with new_uow() as uow:
        await uow.sessions.add(owner)
        await uow.tasks.add(task(owner, event="ev-dup"))
        await uow.commit()
    try:
        async with new_uow() as uow:
            await uow.tasks.add(task(owner, event="ev-dup"))
            await uow.commit()
    except ConcurrentUpdateError:
        return
    raise AssertionError("a second task for the same event must be rejected")


async def check_claim_order_and_engine(new_uow: UnitOfWorkFactory) -> None:
    first, second = session(thread="1"), session(thread="2")
    gemini = session(thread="3", engine=Engine.GEMINI)
    older = task(first, now=T0)
    newer = task(second, now=T0 + timedelta(seconds=1))
    other = task(gemini, now=T0 - timedelta(seconds=1))
    async with new_uow() as uow:
        for s in (first, second, gemini):
            await uow.sessions.add(s)
        for t in (older, newer, other):
            await uow.tasks.add(t)
        await uow.commit()
    async with new_uow() as uow:
        claimed = await uow.queue.claim_next(Engine.CLAUDE)
        _expect(claimed is not None and claimed.id == older.id, "oldest claude task first")


async def check_one_running_task_per_session(new_uow: UnitOfWorkFactory) -> None:
    owner = session()
    first = task(owner, now=T0)
    second = task(owner, now=T0 + timedelta(seconds=1))
    async with new_uow() as uow:
        await uow.sessions.add(owner)
        await uow.tasks.add(first)
        await uow.tasks.add(second)
        await uow.commit()
    async with new_uow() as uow:
        claimed = await uow.queue.claim_next(Engine.CLAUDE)
        if claimed is None or claimed.id != first.id:
            raise AssertionError("first task of the session")
        lease_id = uuid.uuid4()
        await uow.tasks.update(
            claimed.grant_lease(lease_id=lease_id, now=T0, duration=timedelta(minutes=1))
        )
        await uow.commit()
    async with new_uow() as uow:
        _expect(await uow.queue.claim_next(Engine.CLAUDE) is None, "session busy (I-29)")
    async with new_uow() as uow:
        running = await uow.tasks.get_for_update(first.id)
        _expect(running is not None, "running task exists")
        if running is not None:
            done = running.succeed(lease_id=lease_id, text="ok", now=T0)
            await uow.tasks.update(done)
        await uow.commit()
    async with new_uow() as uow:
        claimed = await uow.queue.claim_next(Engine.CLAUDE)
        _expect(claimed is not None and claimed.id == second.id, "next task after completion")


async def check_claim_skips_locked(new_uow: UnitOfWorkFactory) -> None:
    first, second = session(thread="1"), session(thread="2")
    a = task(first, now=T0)
    b = task(second, now=T0 + timedelta(seconds=1))
    async with new_uow() as uow:
        await uow.sessions.add(first)
        await uow.sessions.add(second)
        await uow.tasks.add(a)
        await uow.tasks.add(b)
        await uow.commit()
    async with new_uow() as one, new_uow() as two:
        got_one = await one.queue.claim_next(Engine.CLAUDE)
        got_two = await two.queue.claim_next(Engine.CLAUDE)
        _expect(got_one is not None and got_one.id == a.id, "first worker gets oldest")
        _expect(got_two is not None and got_two.id == b.id, "second worker skips locked task")


async def check_claim_skips_locked_session(new_uow: UnitOfWorkFactory) -> None:
    owner = session()
    a = task(owner, now=T0)
    async with new_uow() as uow:
        await uow.sessions.add(owner)
        await uow.tasks.add(a)
        await uow.commit()
    async with new_uow() as one, new_uow() as two:
        _expect(await one.queue.claim_next(Engine.CLAUDE) is not None, "first worker claims")
        _expect(await two.queue.claim_next(Engine.CLAUDE) is None, "session locked elsewhere")


async def check_claim_expired(new_uow: UnitOfWorkFactory) -> None:
    first, second = session(thread="1"), session(thread="2")
    short = task(first).grant_lease(lease_id=uuid.uuid4(), now=T0, duration=timedelta(seconds=10))
    long = task(second).grant_lease(lease_id=uuid.uuid4(), now=T0, duration=timedelta(hours=1))
    async with new_uow() as uow:
        await uow.sessions.add(first)
        await uow.sessions.add(second)
        await uow.tasks.add(short)
        await uow.tasks.add(long)
        await uow.commit()
    async with new_uow() as uow:
        expired = await uow.queue.claim_expired(T0 + timedelta(seconds=30), 10)
        _expect([t.id for t in expired] == [short.id], "only the expired lease")


async def check_transcripts(new_uow: UnitOfWorkFactory) -> None:
    owner, other = session(thread="1"), session(thread="2")
    main = TranscriptKey(project_key="p", engine_session_id="s1")
    sub = TranscriptKey(project_key="p", engine_session_id="s1", subpath="subagents/agent-1")
    first: tuple[TranscriptEntry, ...] = (
        {"uuid": "a", "type": "user"},
        {"type": "meta"},
        {"uuid": "b", "nested": {"values": [1, 2.5, None, True, "x"]}},
    )
    async with new_uow() as uow:
        await uow.sessions.add(owner)
        await uow.sessions.add(other)
        _expect(await uow.transcripts.append(owner.id, main, first) == 3, "first batch stored")
        await uow.commit()
    async with new_uow() as uow:
        again: tuple[TranscriptEntry, ...] = ({"uuid": "b"}, {"uuid": "c"}, {"uuid": "c"})
        _expect(await uow.transcripts.append(owner.id, main, again) == 1, "duplicates skipped")
        await uow.transcripts.append(owner.id, sub, ({"uuid": "a"},))
        await uow.transcripts.append(other.id, main, ({"uuid": "z"},))
        await uow.commit()
    async with new_uow() as uow:
        await uow.transcripts.append(owner.id, main, ({"uuid": "rolled-back"},))
    async with new_uow() as uow:
        loaded = list(await uow.transcripts.load(owner.id, main))
        _expect(loaded == [*first, {"uuid": "c"}], "append order without duplicates")
        _expect(list(await uow.transcripts.load(owner.id, sub)) == [{"uuid": "a"}], "subpath")
        _expect(list(await uow.transcripts.load(other.id, main)) == [{"uuid": "z"}], "per session")
        subpaths = list(await uow.transcripts.subpaths(owner.id, "p", "s1"))
        _expect(subpaths == ["subagents/agent-1"], "subpaths")
        missing = TranscriptKey(project_key="p", engine_session_id="missing")
        _expect(not await uow.transcripts.load(owner.id, missing), "unknown transcript is empty")


async def check_isolated_tasks_stay_out_of_shared_queue(new_uow: UnitOfWorkFactory) -> None:
    shared_owner, isolated_owner = session(thread="1"), session(thread="2")
    isolated = task(isolated_owner, now=T0 - timedelta(seconds=5), execution=Execution.ISOLATED)
    shared = task(shared_owner, now=T0)
    async with new_uow() as uow:
        await uow.sessions.add(shared_owner)
        await uow.sessions.add(isolated_owner)
        await uow.tasks.add(isolated)
        await uow.tasks.add(shared)
        await uow.commit()
    async with new_uow() as uow:
        claimed = await uow.queue.claim_next(Engine.CLAUDE)
        _expect(claimed is not None and claimed.id == shared.id, "shared queue skips isolated")
    async with new_uow() as uow:
        _expect(await uow.queue.claim_next(Engine.CLAUDE) is not None, "shared task again")
        found = await uow.queue.next_isolated()
        _expect(found is not None and found.id == isolated.id, "oldest isolated task")
        _expect(await uow.queue.claim_isolated(shared.id) is None, "shared task not claimable")
    async with new_uow() as uow:
        claimed = await uow.queue.claim_isolated(isolated.id)
        _expect(claimed is not None and claimed.id == isolated.id, "isolated task claimed")


async def check_delegated_session_round_trip(new_uow: UnitOfWorkFactory) -> None:
    origin = conversation("1.0")
    delegated = Session.start(
        session_id=uuid.uuid4(),
        conversation=conversation("1.0:developer"),
        agent=agent("developer", execution=Execution.ISOLATED),
        now=T0,
        reply_to=origin,
    )
    async with new_uow() as uow:
        await uow.sessions.add(delegated)
        await uow.commit()
    async with new_uow() as uow:
        found = await uow.sessions.get(delegated.id)
        _expect(found == delegated, "delegated session round trip")
        _expect(found is not None and found.outbox_conversation == origin, "replies upstream")
    first = task(delegated, now=T0)
    second = task(delegated, now=T0 + timedelta(minutes=5))
    async with new_uow() as uow:
        _expect(await uow.tasks.latest_in_session(delegated.id) is None, "no task yet")
        await uow.tasks.add(second)
        await uow.tasks.add(first)
        await uow.commit()
    async with new_uow() as uow:
        latest = await uow.tasks.latest_in_session(delegated.id)
        _expect(latest is not None and latest.id == second.id, "latest task of a session")
    owner = session(thread="1.0")
    own = task(owner, now=T0)
    async with new_uow() as uow:
        await uow.sessions.add(owner)
        await uow.tasks.add(own)
        for at, related in (
            (T0, second),
            (T0 + timedelta(minutes=1), own),
            (T0 + timedelta(minutes=2), second),
        ):
            await uow.outbox.add(
                outbox_message(delegated, related, now=at).model_copy(
                    update={"conversation": origin}
                )
            )
        await uow.commit()
    async with new_uow() as uow:
        others = await uow.outbox.posted_by_others(origin, owner.id, T0 - timedelta(1), 10)
        _expect([m.created_at for m in others] == [T0, T0 + timedelta(minutes=2)], "others")
        newest = await uow.outbox.posted_by_others(origin, owner.id, T0, 10)
        _expect(len(newest) == 1, "only after since")
        capped = await uow.outbox.posted_by_others(origin, owner.id, T0 - timedelta(1), 1)
        _expect([m.created_at for m in capped] == [T0 + timedelta(minutes=2)], "newest kept")


async def check_dispatches(new_uow: UnitOfWorkFactory) -> None:
    owner = session()
    work = task(owner, execution=Execution.ISOLATED)
    async with new_uow() as uow:
        await uow.sessions.add(owner)
        await uow.tasks.add(work)
        for offset in (0, 2, 30):
            await uow.dispatches.add(
                Dispatch(
                    id=uuid.uuid4(),
                    task_id=work.id,
                    job_name=f"enikk-dev-{offset}",
                    created_at=T0 - timedelta(hours=offset),
                )
            )
        await uow.commit()
    async with new_uow() as uow:
        _expect(await uow.dispatches.count_since(T0 - timedelta(hours=24)) == 2, "last 24h")
        _expect(await uow.dispatches.count_for(work.id) == 3, "per task")
        _expect(await uow.dispatches.count_for(uuid.uuid4()) == 0, "other task")


def approval(number: int = 1, sha: str = "a" * 40, *, now: datetime = T0) -> Approval:
    return Approval(
        id=uuid.uuid4(),
        repository="404Vector/project-enikk",
        number=number,
        head_sha=sha,
        branch=f"agent/{number}",
        title=f"PR {number}",
        url=f"https://github.com/404Vector/project-enikk/pull/{number}",
        state=ApprovalState.REQUESTED,
        created_at=now,
    )


async def check_approvals_and_notices(new_uow: UnitOfWorkFactory) -> None:
    first = approval(1)
    second = approval(2, "b" * 40, now=T0 + timedelta(seconds=1))
    notice = Notice(
        id=uuid.uuid4(),
        body=ChecksFailed(
            pull=PullRequestCard(
                repository="404Vector/project-enikk",
                number=1,
                url=first.url,
                title=first.title,
                author="enikk-deployer[bot]",
                branch=first.branch,
                head_sha=first.head_sha,
            )
        ),
        created_at=T0,
    )
    async with new_uow() as uow:
        await uow.approvals.add(first)
        await uow.approvals.add(second)
        await uow.notices.add(notice)
        await uow.commit()
    async with new_uow() as uow:
        found = await uow.approvals.find("404Vector/project-enikk", 1, "a" * 40)
        _expect(found == first, "approval by commit")
        locked = await uow.approvals.get_for_update(second.id)
        _expect(locked == second, "approval by id")
        requested = list(await uow.approvals.in_state(ApprovalState.REQUESTED))
        _expect(requested == [first, second], "approvals in state, oldest first")
        merged = first.resolve(ApprovalState.MERGED, T0, by="U1")
        await uow.approvals.update(merged)
        _expect(list(await uow.notices.pending(10)) == [notice], "pending notice")
        await uow.notices.update(notice.mark_delivered())
        await uow.commit()
    async with new_uow() as uow:
        _expect(await uow.approvals.get_for_update(first.id) == merged, "approval update")
        _expect(not await uow.notices.pending(10), "delivered notice not pending")
        _expect(len(await uow.notices.get_many([notice.id])) == 1, "notice by id")
    try:
        async with new_uow() as uow:
            await uow.approvals.add(approval(1))
            await uow.commit()
    except ConcurrentUpdateError:
        return
    raise AssertionError("a second approval for the same commit must be rejected")


async def check_deployment_watch(new_uow: UnitOfWorkFactory) -> None:
    async with new_uow() as uow:
        _expect(await uow.deployment_watch.load_for_update() is None, "no watch before save")
    first = DeploymentWatch(
        baselines=(
            AppBaseline(app="enikk-core", image_tag="sha-" + "a" * 40, operation_started_at=T0),
        ),
        seen_build_runs=(1, 2),
        builds_since=T0,
    )
    async with new_uow() as uow:
        await uow.deployment_watch.save(first)
        await uow.commit()
    second = first.saw_build_runs([3])
    async with new_uow() as uow:
        _expect(await uow.deployment_watch.load_for_update() == first, "watch round trip")
        await uow.deployment_watch.save(second)
        await uow.commit()
    async with new_uow() as uow:
        await uow.deployment_watch.save(DeploymentWatch())
    async with new_uow() as uow:
        _expect(await uow.deployment_watch.load_for_update() == second, "watch update")


def vault_note(path: str, body: str = "body", sha: str = "s1") -> VaultNote:
    from enikk_contracts.memory import NoteKind

    return VaultNote(
        path=path,
        blob_sha=sha,
        title=path.rsplit("/", 1)[-1],
        kind=NoteKind.NOTE,
        body=body,
        synced_at=T0,
    )


async def check_vault_index(new_uow: UnitOfWorkFactory) -> None:
    first = vault_note("vault/Private/enikk/선호/알림.md", "배포 알림은 #release 로")
    second = vault_note("vault/Garden/Kubernetes.md", "Argo CD 와 Release 흐름")
    async with new_uow() as uow:
        await uow.vault.upsert(first)
        await uow.vault.upsert(second)
        await uow.commit()
    async with new_uow() as uow:
        _expect(await uow.vault.shas() == {first.path: "s1", second.path: "s1"}, "shas")
        await uow.vault.upsert(first.model_copy(update={"blob_sha": "s2"}))
        under = await uow.vault.under("vault/Private/enikk/")
        _expect([n.path for n in under] == [first.path], "under a prefix")
        found = await uow.vault.search(["RELEASE"], 10)
        _expect({n.path for n in found} == {first.path, second.path}, "search ignores case")
        _expect(
            [n.path for n in await uow.vault.search(["argo", "release"], 10)] == [second.path],
            "every word must match",
        )
        await uow.vault.remove([second.path])
        await uow.commit()
    async with new_uow() as uow:
        _expect(await uow.vault.shas() == {first.path: "s2"}, "update and remove")


async def check_review_messages(new_uow: UnitOfWorkFactory) -> None:
    first = ReviewMessageRef(repository="404Vector/project-enikk", number=7, channel="D1", ts="1.0")
    async with new_uow() as uow:
        _expect(await uow.review_messages.get(first.repository, 7) is None, "none yet")
        await uow.review_messages.put(first)
        await uow.commit()
    async with new_uow() as uow:
        _expect(await uow.review_messages.get(first.repository, 7) == first, "round trip")
        await uow.review_messages.put(first.model_copy(update={"ts": "2.0"}))
        await uow.commit()
    async with new_uow() as uow:
        found = await uow.review_messages.get(first.repository, 7)
        _expect(found is not None and found.ts == "2.0", "replaced")


CONFORMANCE_CHECKS: tuple[Callable[[UnitOfWorkFactory], Awaitable[None]], ...] = (
    check_round_trip,
    check_updates_persist,
    check_rollback_without_commit,
    check_duplicate_event_rejected,
    check_claim_order_and_engine,
    check_one_running_task_per_session,
    check_claim_skips_locked,
    check_claim_skips_locked_session,
    check_claim_expired,
    check_transcripts,
    check_isolated_tasks_stay_out_of_shared_queue,
    check_dispatches,
    check_delegated_session_round_trip,
    check_approvals_and_notices,
    check_deployment_watch,
    check_vault_index,
    check_review_messages,
)
