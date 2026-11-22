"""Starts one Job per isolated task, within the concurrency and daily limits (ADR-0021)."""

from __future__ import annotations

import logging
from datetime import timedelta
from enum import StrEnum
from typing import TYPE_CHECKING

from enikk_contracts.outbox import OutboundKind
from enikk_core_domain.errors import DependencyUnavailableError
from enikk_core_domain.model import Dispatch, OutboxMessage

if TYPE_CHECKING:
    from enikk_core.settings import DevAgentSettings
    from enikk_core_domain.model import Task
    from enikk_core_domain.ports import JobLauncher, UnitOfWork, UnitOfWorkFactory
    from enikk_kernel.clock import Clock
    from enikk_kernel.ids import IdGenerator

_log = logging.getLogger(__name__)

DAY = timedelta(hours=24)


class DispatchOutcome(StrEnum):
    IDLE = "idle"
    """No isolated task is waiting."""
    NOT_DEPLOYED = "not-deployed"
    BUSY = "busy"
    """The concurrency limit is reached."""
    DAILY_LIMIT = "daily-limit"
    LAUNCHED = "launched"
    ABANDONED = "abandoned"
    """The task used up its Job starts without a worker reporting back."""
    FAILED = "failed"
    """The Job could not be created; retried on the next round."""


class DevJobDispatcher:
    def __init__(
        self,
        *,
        uow: UnitOfWorkFactory,
        launcher: JobLauncher,
        clock: Clock,
        ids: IdGenerator,
        policy: DevAgentSettings,
    ) -> None:
        self._uow = uow
        self._launcher = launcher
        self._clock = clock
        self._ids = ids
        self._policy = policy

    async def dispatch_once(self) -> DispatchOutcome:
        async with self._uow() as uow:
            task = await uow.queue.next_isolated()
            if task is None:
                return DispatchOutcome.IDLE
            if not await self._launcher.ready():
                return DispatchOutcome.NOT_DEPLOYED
            if await self._launcher.running() >= self._policy.max_concurrent:
                return DispatchOutcome.BUSY
            now = self._clock.now()
            if await uow.dispatches.count_for(task.id) >= task.max_attempts:
                await self._abandon(uow, task)
                return DispatchOutcome.ABANDONED
            if await uow.dispatches.count_since(now - DAY) >= self._policy.daily_limit:
                _log.warning("daily limit of %d dev Jobs reached", self._policy.daily_limit)
                return DispatchOutcome.DAILY_LIMIT
            try:
                job_name = await self._launcher.launch(task.id)
            except DependencyUnavailableError as exc:
                _log.warning("could not start a Job for task %s: %s", task.id, exc)
                return DispatchOutcome.FAILED
            await uow.dispatches.add(
                Dispatch(id=self._ids.new(), task_id=task.id, job_name=job_name, created_at=now)
            )
            await uow.commit()
        _log.info("started Job %s for task %s", job_name, task.id)
        return DispatchOutcome.LAUNCHED

    async def _abandon(self, uow: UnitOfWork, task: Task) -> None:
        now = self._clock.now()
        reason = f"no isolated worker reported back after {task.max_attempts} Job starts"
        failed = task.abandon(reason=reason, now=now)
        await uow.tasks.update(failed)
        session = await uow.sessions.get(task.session_id)
        if session is not None:
            await uow.outbox.add(
                OutboxMessage(
                    id=self._ids.new(),
                    task_id=task.id,
                    source_event_id=task.source_event_id,
                    conversation=session.conversation,
                    kind=OutboundKind.FAILURE,
                    text=reason,
                    created_at=now,
                )
            )
        await uow.commit()
        _log.warning("task %s abandoned: %s", task.id, reason)
