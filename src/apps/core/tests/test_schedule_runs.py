"""Running schedules on the scheduler's clock (ADR-0039 S5)."""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import TYPE_CHECKING

import pytest

from enikk_contracts.outbox import OutboundKind
from enikk_contracts.schedules import OutputKind, RunStatus, ScheduleOutput, Timing
from enikk_contracts.tasks import TaskStatus
from enikk_core.schedule_runs import ScheduleRunner
from enikk_core_domain.schedules import Schedule
from enikk_core_domain.testing import conversation, schedule_spec
from enikk_kernel.ids import RandomIdGenerator

if TYPE_CHECKING:
    from conftest import Harness

    from enikk_core_domain.model import Task

pytestmark = pytest.mark.anyio


async def _schedule(
    harness: Harness, output: OutputKind = OutputKind.THREAD, retries: int = 1
) -> Schedule:
    due = harness.clock.now() + timedelta(minutes=1)
    spec = schedule_spec(agent="assistant").model_copy(
        update={
            "timing": Timing(at=due),
            "output": ScheduleOutput(kind=output),
            "retries": retries,
        }
    )
    schedule = Schedule.create(
        schedule_id=uuid.uuid4(),
        name="news",
        spec=spec,
        owner_agent="assistant",
        origin=conversation("7.0"),
        approval=False,
        now=harness.clock.now(),
    )
    async with harness.db.unit_of_work() as uow:
        await uow.schedules.add(schedule)
        await uow.commit()
    return schedule


def _runner(harness: Harness) -> ScheduleRunner:
    return ScheduleRunner(
        uow=harness.db.unit_of_work,
        catalog=harness.catalog,
        clock=harness.clock,
        ids=RandomIdGenerator(),
    )


def _queued(harness: Harness) -> list[Task]:
    return [t for t in harness.db.tasks.values() if t.status is TaskStatus.QUEUED]


async def _end(harness: Harness, task: Task, *, ok: bool) -> None:
    update = (
        {"status": TaskStatus.SUCCEEDED, "output": "오늘의 뉴스"}
        if ok
        else {"status": TaskStatus.FAILED, "failure": "boom"}
    )
    async with harness.db.unit_of_work() as uow:
        await uow.tasks.update(task.model_copy(update=update))
        await uow.commit()


async def test_a_due_run_starts_once_and_its_answer_goes_to_the_thread(harness: Harness) -> None:
    schedule = await _schedule(harness)
    runner = _runner(harness)
    assert (await runner.tick()).started == 0
    harness.clock.advance(timedelta(minutes=2))
    assert (await runner.tick()).started == 1
    assert (await runner.tick()).started == 0
    [task] = _queued(harness)
    assert "[스케줄 `news`]" in task.input
    assert "skill-news" in task.input
    await _end(harness, task, ok=True)
    assert (await runner.tick()).settled == 1
    [message] = [m for m in harness.db.outbox.values() if m.kind is OutboundKind.REPLY]
    assert message.conversation == schedule.origin
    assert message.text.endswith("오늘의 뉴스")
    async with harness.db.unit_of_work() as uow:
        [run] = await uow.schedule_runs.latest(schedule.id, 5)
    assert run.status is RunStatus.SUCCEEDED


async def test_a_failure_is_retried_then_recorded(harness: Harness) -> None:
    schedule = await _schedule(harness, retries=1)
    runner = _runner(harness)
    harness.clock.advance(timedelta(minutes=2))
    await runner.tick()
    [first] = _queued(harness)
    await _end(harness, first, ok=False)
    await runner.tick()
    [second] = _queued(harness)
    assert second.id != first.id
    await _end(harness, second, ok=False)
    await runner.tick()
    async with harness.db.unit_of_work() as uow:
        [run] = await uow.schedule_runs.latest(schedule.id, 5)
    assert (run.status, run.attempts, run.detail) == (RunStatus.FAILED, 2, "boom")


async def test_outputs_of_later_stages_fail_the_run(harness: Harness) -> None:
    schedule = await _schedule(harness, output=OutputKind.OWNER_DM)
    harness.clock.advance(timedelta(minutes=2))
    await _runner(harness).tick()
    assert _queued(harness) == []
    async with harness.db.unit_of_work() as uow:
        [run] = await uow.schedule_runs.latest(schedule.id, 5)
    assert run.status is RunStatus.FAILED
    assert "S7" in run.detail


async def test_run_now_starts_a_run_at_once(harness: Harness) -> None:
    schedule = await _schedule(harness)
    run = await _runner(harness).run_now(schedule)
    assert run.status is RunStatus.RUNNING
    assert run.slot == harness.clock.now()
    assert len(_queued(harness)) == 1
