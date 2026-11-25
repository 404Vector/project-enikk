"""Schedules (ADR-0039): what the scheduler service reads and changes on a task's behalf.

Every request names the calling session (or task and lease). Core finds the task with
a live lease and takes the agent and session from it. Any session may change schedules
and nothing waits for approval; a schedule's tools never exceed its agent's definition
(ADR-0041 §1, I-39).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from enikk_contracts.schedules import (
    MAX_LISTED,
    RunList,
    ScheduleDeleted,
    ScheduleList,
)
from enikk_core_domain.errors import NotFoundError, PermissionDeniedError
from enikk_core_domain.schedules import Schedule, check_spec

if TYPE_CHECKING:
    from enikk_contracts.agents import AgentDefinition
    from enikk_contracts.schedules import (
        Caller,
        RunsQuery,
        ScheduleCreate,
        SchedulePause,
        ScheduleQuery,
        ScheduleSpec,
        ScheduleUpdate,
        ScheduleView,
    )
    from enikk_core_domain.model import Session, Task
    from enikk_core_domain.ports import AgentCatalog, UnitOfWork, UnitOfWorkFactory
    from enikk_kernel.clock import Clock
    from enikk_kernel.ids import IdGenerator


class ScheduleService:
    def __init__(
        self, *, uow: UnitOfWorkFactory, catalog: AgentCatalog, clock: Clock, ids: IdGenerator
    ) -> None:
        self._uow = uow
        self._catalog = catalog
        self._clock = clock
        self._ids = ids

    async def list(self, query: ScheduleQuery) -> ScheduleList:
        async with self._uow() as uow:
            await self._caller(uow, query.caller)
            schedules = await uow.schedules.all(MAX_LISTED)
        return ScheduleList(schedules=tuple(s.view() for s in schedules))

    async def get(self, name: str, query: ScheduleQuery) -> ScheduleView:
        async with self._uow() as uow:
            await self._caller(uow, query.caller)
            schedule = await self._named(uow, name)
        return schedule.view()

    async def runs(self, name: str, query: RunsQuery) -> RunList:
        async with self._uow() as uow:
            await self._caller(uow, query.caller)
            schedule = await self._named(uow, name)
            runs = await uow.schedule_runs.latest(schedule.id, query.limit)
        return RunList(runs=tuple(r.view() for r in runs))

    async def create(self, request: ScheduleCreate) -> ScheduleView:
        async with self._uow() as uow:
            task, session = await self._caller(uow, request.caller)
            spec, _ = self._checked(request.spec)
            schedule = Schedule.create(
                schedule_id=self._ids.new(),
                name=request.name,
                spec=spec,
                owner_agent=self._agent_name(task.agent),
                origin=session.outbox_conversation,
                approval=False,
                now=self._clock.now(),
            )
            await uow.schedules.add(schedule)
            await uow.commit()
        return schedule.view()

    async def update(self, name: str, request: ScheduleUpdate) -> ScheduleView:
        async with self._uow() as uow:
            await self._caller(uow, request.caller)
            spec, _ = self._checked(request.spec)
            schedule = await self._named(uow, name)
            changed = schedule.replace(spec, approval=False, now=self._clock.now())
            await uow.schedules.update(changed)
            await uow.commit()
        return changed.view()

    async def pause(self, name: str, request: SchedulePause) -> ScheduleView:
        async with self._uow() as uow:
            await self._caller(uow, request.caller)
            schedule = await self._named(uow, name)
            now = self._clock.now()
            changed = schedule.pause(now) if request.paused else schedule.resume(now)
            await uow.schedules.update(changed)
            await uow.commit()
        return changed.view()

    async def delete(self, name: str, query: ScheduleQuery) -> ScheduleDeleted:
        async with self._uow() as uow:
            await self._caller(uow, query.caller)
            schedule = await self._named(uow, name)
            await uow.schedules.delete(schedule.id)
            await uow.commit()
        return ScheduleDeleted(id=schedule.id)

    async def _caller(self, uow: UnitOfWork, caller: Caller) -> tuple[Task, Session]:
        task = await self._calling_task(uow, caller)
        session = await uow.sessions.get(task.session_id)
        if session is None:
            raise NotFoundError(f"session {task.session_id} of task {task.id} is missing")
        return task, session

    async def _calling_task(self, uow: UnitOfWork, caller: Caller) -> Task:
        """The caller's task with a live lease: the session's running task (ADR-0041 §3)
        or the named task."""
        now = self._clock.now()
        if caller.session_id is not None:
            for running in await uow.tasks.active_in_session(caller.session_id):
                if running.lease is not None and running.lease.expires_at > now:
                    return running
            raise PermissionDeniedError(f"session {caller.session_id} has no running task")
        if caller.task_id is None or caller.lease_id is None:
            raise PermissionDeniedError("the caller names no session and no task")
        task = await uow.tasks.get_for_update(caller.task_id)
        if task is None:
            raise PermissionDeniedError(f"task {caller.task_id} does not exist")
        task.verify_lease(caller.lease_id, now)
        return task

    def _checked(self, spec: ScheduleSpec) -> tuple[ScheduleSpec, AgentDefinition]:
        definition = check_spec(spec, self._catalog.get(spec.agent))
        return spec.model_copy(update={"agent": definition.name}), definition

    def _agent_name(self, agent: str) -> str:
        definition = self._catalog.get(agent)
        return definition.name if definition else agent

    @staticmethod
    async def _named(uow: UnitOfWork, name: str) -> Schedule:
        schedule = await uow.schedules.get_for_update(name)
        if schedule is None:
            raise NotFoundError(f"schedule {name} does not exist")
        return schedule
