"""Core use cases. Each runs in one unit of work and returns a contract model."""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import TYPE_CHECKING

from enikk_contracts.events import EventAccepted
from enikk_contracts.outbox import OutboundKind, OutboundMessage, OutboxAcked, OutboxBatch
from enikk_contracts.tasks import (
    LeaseExtended,
    LeaseResponse,
    ResultAccepted,
    Succeeded,
    TaskAssignment,
    TaskStatus,
)
from enikk_core_domain.errors import NotFoundError, UnknownAgentError
from enikk_core_domain.model import OutboxMessage, Session, Task

if TYPE_CHECKING:
    import uuid
    from collections.abc import Awaitable, Callable

    from enikk_contracts.common import ConnectorKind, Engine
    from enikk_contracts.events import InboundEvent
    from enikk_contracts.outbox import OutboxAck, OutboxPoll
    from enikk_contracts.tasks import Heartbeat, LeaseRequest, ResultReport
    from enikk_core.settings import TaskSettings
    from enikk_core_domain.ports import AgentCatalog, UnitOfWork, UnitOfWorkFactory
    from enikk_kernel.clock import Clock
    from enikk_kernel.ids import IdGenerator

    Sleep = Callable[[float], Awaitable[None]]

_log = logging.getLogger(__name__)


class CoreUseCases:
    def __init__(
        self,
        *,
        uow: UnitOfWorkFactory,
        catalog: AgentCatalog,
        clock: Clock,
        ids: IdGenerator,
        policy: TaskSettings,
        sleep: Sleep,
    ) -> None:
        self._uow = uow
        self._catalog = catalog
        self._clock = clock
        self._ids = ids
        self._policy = policy
        self._sleep = sleep

    # ------------------------------------------------------------ connector

    async def accept_event(self, event: InboundEvent) -> EventAccepted:
        async with self._uow() as uow:
            existing = await uow.tasks.find_by_source_event(event.event_id)
            if existing is not None:
                return EventAccepted(
                    task_id=existing.id,
                    session_id=existing.session_id,
                    agent=existing.agent,
                    duplicate=True,
                )
            now = self._clock.now()
            session = await uow.sessions.find_by_conversation(event.conversation)
            if session is None:
                definition = self._catalog.get(event.agent)
                if definition is None:
                    raise UnknownAgentError(f"agent '{event.agent}' is not defined")
                session = Session.start(
                    session_id=self._ids.new(),
                    conversation=event.conversation,
                    agent=definition,
                    now=now,
                )
                await uow.sessions.add(session)
            task = Task.enqueue(
                task_id=self._ids.new(),
                session=session,
                source_event_id=event.event_id,
                text=event.text,
                max_attempts=self._policy.max_attempts,
                now=now,
            )
            await uow.tasks.add(task)
            await uow.commit()
        _log.info("task %s queued for agent %s", task.id, task.agent)
        return EventAccepted(
            task_id=task.id, session_id=session.id, agent=session.agent, duplicate=False
        )

    async def poll_outbox(self, connector: ConnectorKind, poll: OutboxPoll) -> OutboxBatch:
        deadline = self._clock.now() + timedelta(seconds=poll.wait_seconds)
        while True:
            async with self._uow() as uow:
                pending = await uow.outbox.pending(connector, poll.limit)
            if pending or self._clock.now() >= deadline:
                return OutboxBatch(messages=tuple(_outbound(m) for m in pending))
            await self._sleep(self._policy.poll_interval_seconds)

    async def ack_outbox(self, connector: ConnectorKind, ack: OutboxAck) -> OutboxAcked:
        async with self._uow() as uow:
            now = self._clock.now()
            changed = 0
            for message in await uow.outbox.get_many(connector, ack.message_ids):
                delivered = message.mark_delivered(now)
                if delivered is not message:
                    await uow.outbox.update(delivered)
                    changed += 1
            await uow.commit()
        return OutboxAcked(acknowledged=changed)

    # ------------------------------------------------------------ runtime

    async def lease_task(self, engine: Engine, request: LeaseRequest) -> LeaseResponse:
        deadline = self._clock.now() + timedelta(seconds=request.wait_seconds)
        while True:
            assignment = await self._try_lease(engine)
            if assignment is not None or self._clock.now() >= deadline:
                return LeaseResponse(assignment=assignment)
            await self._sleep(self._policy.poll_interval_seconds)

    async def _try_lease(self, engine: Engine) -> TaskAssignment | None:
        async with self._uow() as uow:
            queued = await uow.queue.claim_next(engine)
            if queued is None:
                return None
            session = await uow.sessions.get(queued.session_id)
            if session is None:
                raise NotFoundError(f"session {queued.session_id} of task {queued.id} is missing")
            leased = queued.grant_lease(
                lease_id=self._ids.new(),
                now=self._clock.now(),
                duration=self._policy.lease_duration,
            )
            await uow.tasks.update(leased)
            await uow.commit()
        if leased.lease is None or leased.last_lease_id is None:
            raise RuntimeError("leased task without a lease")
        _log.info("task %s leased (attempt %d)", leased.id, leased.attempt)
        return TaskAssignment(
            task_id=leased.id,
            lease_id=leased.last_lease_id,
            lease_expires_at=leased.lease.expires_at,
            attempt=leased.attempt,
            agent=leased.agent,
            session_id=session.id,
            engine_session_id=session.engine_session_id,
            input=leased.input,
        )

    async def heartbeat(self, engine: Engine, task_id: uuid.UUID, beat: Heartbeat) -> LeaseExtended:
        async with self._uow() as uow:
            task = await self._task_of(uow, engine, task_id)
            extended = task.extend_lease(
                lease_id=beat.lease_id,
                now=self._clock.now(),
                duration=self._policy.lease_duration,
                progress=beat.progress,
            )
            await uow.tasks.update(extended)
            await uow.commit()
        if extended.lease is None:
            raise RuntimeError("extended task without a lease")
        return LeaseExtended(lease_expires_at=extended.lease.expires_at)

    async def report_result(
        self, engine: Engine, task_id: uuid.UUID, report: ResultReport
    ) -> ResultAccepted:
        async with self._uow() as uow:
            task = await self._task_of(uow, engine, task_id)
            if task.already_reported(report.lease_id):
                return ResultAccepted(task_id=task.id, status=task.status, duplicate=True)
            now = self._clock.now()
            outcome = report.outcome
            if isinstance(outcome, Succeeded):
                done = task.succeed(lease_id=report.lease_id, text=outcome.text, now=now)
                if outcome.engine_session_id is not None:
                    session = await self._session_of(uow, done)
                    await uow.sessions.update(
                        session.bind_engine_session(outcome.engine_session_id, now)
                    )
            else:
                done = task.fail(
                    lease_id=report.lease_id,
                    reason=outcome.reason,
                    retryable=outcome.retryable,
                    now=now,
                )
            await uow.tasks.update(done)
            await self._publish_outcome(uow, done)
            await uow.commit()
        _log.info("task %s reported %s", done.id, done.status)
        return ResultAccepted(task_id=done.id, status=done.status, duplicate=False)

    # ------------------------------------------------------------ maintenance

    async def reap_expired_leases(self) -> int:
        async with self._uow() as uow:
            now = self._clock.now()
            expired = await uow.queue.claim_expired(now, self._policy.reaper_batch)
            for task in expired:
                reaped = task.expire(now)
                await uow.tasks.update(reaped)
                await self._publish_outcome(uow, reaped)
                _log.warning("task %s lease expired -> %s", task.id, reaped.status)
            await uow.commit()
        return len(expired)

    # ------------------------------------------------------------ helpers

    async def _task_of(self, uow: UnitOfWork, engine: Engine, task_id: uuid.UUID) -> Task:
        task = await uow.tasks.get_for_update(task_id)
        if task is None or task.engine is not engine:
            raise NotFoundError(f"task {task_id} not found")
        return task

    async def _session_of(self, uow: UnitOfWork, task: Task) -> Session:
        session = await uow.sessions.get(task.session_id)
        if session is None:
            raise NotFoundError(f"session {task.session_id} of task {task.id} is missing")
        return session

    async def _publish_outcome(self, uow: UnitOfWork, task: Task) -> None:
        if task.status is TaskStatus.SUCCEEDED and task.output is not None:
            kind, text = OutboundKind.REPLY, task.output
        elif task.status is TaskStatus.FAILED and task.failure is not None:
            kind, text = OutboundKind.FAILURE, task.failure
        else:
            return
        session = await self._session_of(uow, task)
        await uow.outbox.add(
            OutboxMessage(
                id=self._ids.new(),
                task_id=task.id,
                conversation=session.conversation,
                kind=kind,
                text=text,
                created_at=self._clock.now(),
            )
        )


def _outbound(message: OutboxMessage) -> OutboundMessage:
    return OutboundMessage(
        message_id=message.id,
        task_id=message.task_id,
        conversation=message.conversation,
        kind=message.kind,
        text=message.text,
    )
