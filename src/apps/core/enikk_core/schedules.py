"""Schedules (ADR-0039): what the scheduler service reads and changes on a task's behalf.

Every request names the calling session (or task and lease). Core finds the task with
a live lease and takes the agent and session from it. Any session may change schedules
and nothing waits for approval; a schedule's tools never exceed its agent's definition
(ADR-0041 §1, I-39). An agent sees and manages only the schedules whose agent it may
manage: itself, its delegates, or an agent whose tools it has (ADR-0052).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from enikk_contracts.schedules import (
    MAX_LISTED,
    RunList,
    ScheduleDeleted,
    ScheduleList,
)
from enikk_core.schedule_runs import ScheduleRunner
from enikk_core_domain.errors import NotFoundError, PermissionDeniedError
from enikk_core_domain.schedules import Schedule, check_spec, may_manage

if TYPE_CHECKING:
    from enikk_contracts.agents import AgentDefinition
    from enikk_contracts.schedules import (
        Caller,
        RunNow,
        RunsQuery,
        RunView,
        ScheduleCreate,
        SchedulePause,
        ScheduleQuery,
        ScheduleSpec,
        ScheduleTicked,
        ScheduleUpdate,
        ScheduleView,
    )
    from enikk_core.memory import MemoryService
    from enikk_core_domain.model import Session, Task
    from enikk_core_domain.ports import AgentCatalog, UnitOfWork, UnitOfWorkFactory
    from enikk_kernel.clock import Clock
    from enikk_kernel.ids import IdGenerator


class ScheduleService:
    def __init__(
        self,
        *,
        uow: UnitOfWorkFactory,
        catalog: AgentCatalog,
        clock: Clock,
        ids: IdGenerator,
        memory: MemoryService | None = None,
    ) -> None:
        self._uow = uow
        self._catalog = catalog
        self._clock = clock
        self._ids = ids
        self._runner = ScheduleRunner(uow=uow, catalog=catalog, clock=clock, ids=ids, memory=memory)

    async def tick(self) -> ScheduleTicked:
        """One tick of the scheduler's clock (ADR-0039 S5)."""
        return await self._runner.tick()

    async def run_now(self, name: str, request: RunNow) -> RunView:
        async with self._uow() as uow:
            task, _ = await self._caller(uow, request.caller)
            schedule = await self._managed(uow, name, task)
        run = await self._runner.run_now(schedule)
        return run.view()

    async def list(self, query: ScheduleQuery) -> ScheduleList:
        async with self._uow() as uow:
            task, _ = await self._caller(uow, query.caller)
            schedules = await uow.schedules.all(MAX_LISTED)
        caller = self._catalog.get(task.agent)
        mine = (s for s in schedules if may_manage(caller, self._catalog.get(s.spec.agent)))
        return ScheduleList(schedules=tuple(s.view() for s in mine))

    async def get(self, name: str, query: ScheduleQuery) -> ScheduleView:
        async with self._uow() as uow:
            task, _ = await self._caller(uow, query.caller)
            schedule = await self._managed(uow, name, task)
        return schedule.view()

    async def runs(self, name: str, query: RunsQuery) -> RunList:
        async with self._uow() as uow:
            task, _ = await self._caller(uow, query.caller)
            schedule = await self._managed(uow, name, task)
            runs = await uow.schedule_runs.latest(schedule.id, query.limit)
        return RunList(runs=tuple(r.view() for r in runs))

    async def create(self, request: ScheduleCreate) -> ScheduleView:
        async with self._uow() as uow:
            task, session = await self._caller(uow, request.caller)
            spec, _ = self._checked(request.spec, task)
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
            task, _ = await self._caller(uow, request.caller)
            spec, _ = self._checked(request.spec, task)
            schedule = await self._managed(uow, name, task)
            changed = schedule.replace(spec, approval=False, now=self._clock.now())
            await uow.schedules.update(changed)
            await uow.commit()
        return changed.view()

    async def pause(self, name: str, request: SchedulePause) -> ScheduleView:
        async with self._uow() as uow:
            task, _ = await self._caller(uow, request.caller)
            schedule = await self._managed(uow, name, task)
            now = self._clock.now()
            changed = schedule.pause(now) if request.paused else schedule.resume(now)
            await uow.schedules.update(changed)
            await uow.commit()
        return changed.view()

    async def delete(self, name: str, query: ScheduleQuery) -> ScheduleDeleted:
        async with self._uow() as uow:
            task, _ = await self._caller(uow, query.caller)
            schedule = await self._managed(uow, name, task)
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

    def _checked(self, spec: ScheduleSpec, task: Task) -> tuple[ScheduleSpec, AgentDefinition]:
        definition = check_spec(spec, self._catalog.get(spec.agent))
        self._may_manage(task, definition.name)
        return spec.model_copy(update={"agent": definition.name}), definition

    async def _managed(self, uow: UnitOfWork, name: str, task: Task) -> Schedule:
        schedule = await self._named(uow, name)
        self._may_manage(task, schedule.spec.agent)
        return schedule

    def _may_manage(self, task: Task, agent: str) -> None:
        if not may_manage(self._catalog.get(task.agent), self._catalog.get(agent)):
            raise PermissionDeniedError(
                f"agent '{task.agent}' may not manage schedules of '{agent}' (ADR-0052)"
            )

    def _agent_name(self, agent: str) -> str:
        definition = self._catalog.get(agent)
        return definition.name if definition else agent

    @staticmethod
    async def _named(uow: UnitOfWork, name: str) -> Schedule:
        schedule = await uow.schedules.get_for_update(name)
        if schedule is None:
            raise NotFoundError(f"schedule {name} does not exist")
        return schedule
