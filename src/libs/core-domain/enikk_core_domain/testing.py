"""Test support: in-memory adapters, builders and a port conformance suite.

The conformance checks run against every ``UnitOfWork`` implementation
(in-memory here, SQL in persistence-sql), so all of them behave alike.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Self

from enikk_contracts.agents import AgentDefinition, PromptRef
from enikk_contracts.common import ConnectorKind, ConversationRef, Engine
from enikk_contracts.outbox import OutboundKind
from enikk_contracts.tasks import TaskStatus
from enikk_contracts.transcripts import TranscriptKey
from enikk_core_domain.errors import AuthenticationError, ConcurrentUpdateError
from enikk_core_domain.model import OutboxMessage, OutboxStatus, Session, Task
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


def agent(name: str = "assistant", engine: Engine = Engine.CLAUDE) -> AgentDefinition:
    return AgentDefinition(
        name=name,
        description=f"{name} for tests",
        engine=engine,
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


def task(owner: Session, *, event: str | None = None, now: datetime = T0) -> Task:
    return Task.enqueue(
        task_id=uuid.uuid4(),
        session=owner,
        source_event_id=event or f"ev-{uuid.uuid4()}",
        text="hello",
        max_attempts=3,
        now=now,
    )


def outbox_message(owner: Session, related: Task, *, now: datetime = T0) -> OutboxMessage:
    return OutboxMessage(
        id=uuid.uuid4(),
        task_id=related.id,
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
        self.locks: dict[uuid.UUID, object] = {}

    def unit_of_work(self) -> InMemoryUnitOfWork:
        return InMemoryUnitOfWork(self)


class _Staged:
    def __init__(self, db: InMemoryDatabase) -> None:
        self.sessions = dict(db.sessions)
        self.tasks = dict(db.tasks)
        self.outbox = dict(db.outbox)
        self.transcripts = dict(db.transcripts)


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

    async def add(self, task: Task) -> None:
        self._s.tasks[task.id] = task

    async def update(self, task: Task) -> None:
        self._s.tasks[task.id] = task


class _Outbox:
    def __init__(self, staged: _Staged) -> None:
        self._s = staged

    async def add(self, message: OutboxMessage) -> None:
        self._s.outbox[message.id] = message

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

    async def claim_next(self, engine: Engine) -> Task | None:
        tasks = sorted(self._s.tasks.values(), key=lambda t: (t.created_at, t.id))
        for candidate in tasks:
            if candidate.status is not TaskStatus.QUEUED or candidate.engine is not engine:
                continue
            if self._locked_by_other(candidate.id) or self._locked_by_other(candidate.session_id):
                continue
            siblings = [t for t in tasks if t.session_id == candidate.session_id]
            if any(t.status is TaskStatus.LEASED for t in siblings):
                continue
            if any(
                t.status is TaskStatus.QUEUED
                and (t.created_at, t.id) < (candidate.created_at, candidate.id)
                for t in siblings
            ):
                continue
            self._lock(candidate.id, candidate.session_id)
            return candidate
        return None

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
        self.commits += 1

    def _check_unique(self) -> None:
        sessions = {**self._db.sessions, **self._staged.sessions}.values()
        tasks = {**self._db.tasks, **self._staged.tasks}.values()
        if _has_duplicates([s.conversation for s in sessions]):
            raise ConcurrentUpdateError("a session for this conversation already exists")
        if _has_duplicates([t.source_event_id for t in tasks]):
            raise ConcurrentUpdateError("a task for this event already exists")


def _has_duplicates(values: Sequence[object]) -> bool:
    return len(set(values)) != len(values)


class StaticAgentCatalog:
    def __init__(self, *definitions: AgentDefinition) -> None:
        self._by_name = {d.name: d for d in definitions}

    def get(self, name: str) -> AgentDefinition | None:
        return self._by_name.get(name)

    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._by_name))


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
)
