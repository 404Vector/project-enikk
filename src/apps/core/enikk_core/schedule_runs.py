"""Running schedules (ADR-0039 S5): the scheduler's clock calls ``tick``.

Each tick starts the runs that came due (a new session and task per run, one run per
due time), and settles the runs whose task ended: delivers the answer, retries a
failure up to ``retries`` times, or records the failure. Outputs so far: the thread the
schedule was made in, the owner's DM, a note under enikk's vault folder, or nothing
(S7). The input is the spec's text, or the day before's conversations (S7).
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import TYPE_CHECKING, Final
from zoneinfo import ZoneInfo

from enikk_contracts.common import ConnectorKind, ConversationRef
from enikk_contracts.digests import ScheduleDelivered
from enikk_contracts.memory import NoteKind
from enikk_contracts.outbox import OutboundKind
from enikk_contracts.schedules import (
    MAX_LISTED,
    InputKind,
    OutputKind,
    RunStatus,
    ScheduleState,
    ScheduleTicked,
)
from enikk_contracts.tasks import TaskStatus
from enikk_core.digests import day_conversations
from enikk_core_domain.digests import DigestSchedule, summary_body, summary_input
from enikk_core_domain.model import Notice, OutboxMessage, Session, Task
from enikk_core_domain.schedules import due_slots, started_run

if TYPE_CHECKING:
    from datetime import date, datetime

    from enikk_contracts.agents import AgentDefinition
    from enikk_core.memory import MemoryService
    from enikk_core_domain.ports import AgentCatalog, UnitOfWork, UnitOfWorkFactory
    from enikk_core_domain.schedules import Schedule, ScheduleRun
    from enikk_kernel.clock import Clock
    from enikk_kernel.ids import IdGenerator

EVENT_PREFIX: Final = "schedule:"
CHANNEL: Final = "schedule"
MAX_SETTLED: Final = 100
MAX_ANSWER: Final = 3_000

_log = logging.getLogger(__name__)


class UnsupportedOutputError(Exception):
    """An output or input this core cannot serve (no vault, no folder)."""


class NothingToDoError(Exception):
    """The run has no input: no conversations on the day."""


class ScheduleRunner:
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
        self._memory = memory

    async def tick(self) -> ScheduleTicked:
        now = self._clock.now()
        async with self._uow() as uow:
            schedules = {s.id: s for s in await uow.schedules.all(MAX_LISTED)}
        started = 0
        for schedule in schedules.values():
            if schedule.state is ScheduleState.ACTIVE:
                started += await self._start_due(schedule, now)
        async with self._uow() as uow:
            running = await uow.schedule_runs.running(MAX_SETTLED)
        settled = 0
        for run in running:
            owner = schedules.get(run.schedule_id)
            if owner is not None:
                settled += await self._settle(owner, run, now)
        return ScheduleTicked(started=started, settled=settled)

    async def run_now(self, schedule: Schedule) -> ScheduleRun:
        """Start one run of ``schedule`` now, whatever its timing or state."""
        now = self._clock.now()
        async with self._uow() as uow:
            run = await self._start(uow, schedule, now, now)
            await uow.commit()
        return run

    async def _start_due(self, schedule: Schedule, now: datetime) -> int:
        async with self._uow() as uow:
            latest = await uow.schedule_runs.latest(schedule.id, 1)
            after = latest[0].slot if latest else schedule.created_at
            started = 0
            for slot in due_slots(schedule.spec, after, now):
                if await uow.schedule_runs.get_for_update(schedule.id, slot) is not None:
                    continue
                await self._start(uow, schedule, slot, now)
                started += 1
            await uow.commit()
        return started

    async def _start(
        self, uow: UnitOfWork, schedule: Schedule, slot: datetime, now: datetime
    ) -> ScheduleRun:
        definition = self._catalog.get(schedule.spec.agent)
        if definition is None:
            run = started_run(schedule.id, slot, None, now).ended(
                RunStatus.FAILED, f"agent {schedule.spec.agent} is not defined", now
            )
        else:
            try:
                task = await self._enqueue(uow, schedule, definition, slot, 1, now)
            except UnsupportedOutputError as exc:
                run = started_run(schedule.id, slot, None, now).ended(
                    RunStatus.FAILED, str(exc), now
                )
            except NothingToDoError as exc:
                run = started_run(schedule.id, slot, None, now).ended(
                    RunStatus.SUCCEEDED, str(exc), now
                )
            else:
                run = started_run(schedule.id, slot, task.id, now)
        await uow.schedule_runs.add(run)
        _log.info("schedule %s: run at %s %s", schedule.name, slot.isoformat(), run.status)
        return run

    async def _enqueue(
        self,
        uow: UnitOfWork,
        schedule: Schedule,
        definition: AgentDefinition,
        slot: datetime,
        attempt: int,
        now: datetime,
    ) -> Task:
        spec = schedule.spec
        if spec.output.kind is OutputKind.VAULT_NOTE and (
            self._memory is None or not spec.output.folder
        ):
            raise UnsupportedOutputError("a vault note needs core's vault and a folder")
        text = await self._input(uow, schedule, slot)
        when = slot.isoformat()
        session = Session.start(
            session_id=self._ids.new(),
            conversation=ConversationRef(
                connector=ConnectorKind.SCHEDULE,
                channel=CHANNEL,
                thread=f"{schedule.name}/{when}#{attempt}"[:255],
            ),
            agent=definition,
            now=now,
        )
        await uow.sessions.add(session)
        task = Task.enqueue(
            task_id=self._ids.new(),
            session=session,
            source_event_id=f"{EVENT_PREFIX}{schedule.id}:{when}:{attempt}"[:255],
            text=text,
            max_attempts=1,
            now=now,
            execution=definition.execution,
        )
        await uow.tasks.add(task)
        return task

    async def _settle(self, schedule: Schedule, run: ScheduleRun, now: datetime) -> int:
        async with self._uow() as uow:
            current = await uow.schedule_runs.get_for_update(run.schedule_id, run.slot)
            if current is None or current.status is not RunStatus.RUNNING:
                return 0
            task = (
                None if current.task_id is None else await uow.tasks.get_for_update(current.task_id)
            )
            if task is not None and not task.terminal:
                return 0
            if task is not None and task.status is TaskStatus.SUCCEEDED and task.output:
                try:
                    where = await self._deliver(uow, schedule, task, current.slot, now)
                except Exception as exc:
                    _log.warning("schedule %s: could not deliver: %s", schedule.name, exc)
                    ended = current.ended(RunStatus.FAILED, f"could not deliver: {exc}", now)
                else:
                    ended = current.ended(RunStatus.SUCCEEDED, where, now)
            elif current.attempts <= schedule.spec.retries and task is not None:
                definition = self._catalog.get(schedule.spec.agent)
                if definition is None:
                    ended = current.ended(RunStatus.FAILED, "the agent is gone", now)
                else:
                    retry = await self._enqueue(
                        uow, schedule, definition, current.slot, current.attempts + 1, now
                    )
                    ended = current.retried(retry.id, now)
            else:
                why = "the task is missing" if task is None else (task.failure or task.status)
                ended = current.ended(RunStatus.FAILED, str(why), now)
            await uow.schedule_runs.update(ended)
            await uow.commit()
        _log.info("schedule %s: run at %s %s", schedule.name, run.slot.isoformat(), ended.status)
        return 1

    async def _deliver(
        self, uow: UnitOfWork, schedule: Schedule, task: Task, slot: datetime, now: datetime
    ) -> str:
        """Send the answer where the spec says; returns where, for the run's record."""
        answer = task.output or ""
        output = schedule.spec.output
        short = answer if len(answer) <= MAX_ANSWER else answer[: MAX_ANSWER - 1] + "…"
        if output.kind is OutputKind.THREAD:
            await uow.outbox.add(
                OutboxMessage(
                    id=self._ids.new(),
                    task_id=task.id,
                    source_event_id=task.source_event_id,
                    conversation=schedule.origin,
                    kind=OutboundKind.REPLY,
                    text=f"[스케줄 `{schedule.name}`]\n{short}",
                    created_at=now,
                )
            )
            return ""
        if output.kind is OutputKind.OWNER_DM:
            await uow.notices.add(
                Notice(
                    id=self._ids.new(),
                    body=ScheduleDelivered(name=schedule.name, text=short),
                    created_at=now,
                )
            )
            return ""
        if output.kind is OutputKind.VAULT_NOTE and self._memory is not None and output.folder:
            return await self._note(uow, schedule, answer, slot, output.folder)
        return ""

    async def _note(
        self, uow: UnitOfWork, schedule: Schedule, answer: str, slot: datetime, folder: str
    ) -> str:
        assert self._memory is not None  # noqa: S101 - checked by the caller
        spec = schedule.spec
        if spec.input.kind is InputKind.DAY_CONVERSATIONS:
            day, calendar = _day(schedule, slot)
            threads = await day_conversations(uow, calendar, day)
            return await self._memory.write_scheduled(
                folder,
                day.isoformat(),
                f"{day.isoformat()} {spec.description}",
                summary_body(answer, threads),
                NoteKind.CONVERSATION,
                f"enikk 대화 기록 ({len(threads)}개 스레드)",
            )
        local = slot.astimezone(ZoneInfo(spec.timing.timezone))
        return await self._memory.write_scheduled(
            folder,
            f"{local:%Y-%m-%d-%H%M}-{schedule.name}",
            f"{spec.description} ({local:%Y-%m-%d %H:%M})",
            answer,
            NoteKind.NOTE,
            f"스케줄 {schedule.name}",
        )

    async def _input(self, uow: UnitOfWork, schedule: Schedule, slot: datetime) -> str:
        spec = schedule.spec
        lines = [
            f"[스케줄 `{schedule.name}`] {spec.description}",
            f"- 예정 시각: {slot.isoformat()}",
        ]
        if spec.skill:
            lines.append(f'- 먼저 `load_skill("{spec.skill}")` 로 절차를 불러 따르십시오.')
        lines.append("- 소유자의 메시지가 아니라 예약된 작업입니다. 답이 곧 결과입니다.")
        if spec.input.kind is InputKind.DAY_CONVERSATIONS:
            day, calendar = _day(schedule, slot)
            threads = await day_conversations(uow, calendar, day)
            if not threads:
                raise NothingToDoError(f"no conversations on {day.isoformat()}")
            body = summary_input(day, threads, calendar)
        else:
            body = spec.input.text or spec.description
        return "\n".join([*lines, "", body])


def _day(schedule: Schedule, slot: datetime) -> tuple[date, DigestSchedule]:
    """The day before ``slot`` in the spec's time zone, and that calendar."""
    calendar = DigestSchedule(timezone=schedule.spec.timing.timezone)
    return calendar.today(slot) - timedelta(days=1), calendar
