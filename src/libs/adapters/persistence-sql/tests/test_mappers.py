from __future__ import annotations

import uuid
from datetime import timedelta

from enikk_contracts.schedules import RunStatus
from enikk_core_domain.schedules import Schedule, ScheduleRun
from enikk_core_domain.testing import T0, outbox_message, schedule_spec, session, task
from enikk_persistence_sql import mappers


def test_entities_round_trip_through_rows() -> None:
    owner = session().bind_engine_session("engine-1", T0)
    leased = task(owner).grant_lease(lease_id=uuid.uuid4(), now=T0, duration=timedelta(minutes=1))
    message = outbox_message(owner, leased).mark_delivered(T0)
    assert mappers.session_from_row(mappers.session_to_row(owner)) == owner
    assert mappers.task_from_row(mappers.task_to_row(leased)) == leased
    assert mappers.outbox_from_row(mappers.outbox_to_row(message)) == message


def test_updating_a_row_keeps_its_identity() -> None:
    queued = task(session())
    row = mappers.task_to_row(queued)
    lease_id = uuid.uuid4()
    leased = queued.grant_lease(lease_id=lease_id, now=T0, duration=timedelta(minutes=1))
    assert mappers.task_to_row(leased, row) is row
    assert row.lease_id == lease_id
    assert row.status == "leased"


def test_schedules_round_trip_through_rows() -> None:
    news = Schedule.create(
        schedule_id=uuid.uuid4(),
        name="news",
        spec=schedule_spec(),
        owner_agent="enikk",
        origin=session().conversation,
        approval=True,
        now=T0,
    )
    run = ScheduleRun(
        schedule_id=news.id,
        slot=T0,
        status=RunStatus.FAILED,
        attempts=2,
        detail="timeout",
        started_at=T0,
        updated_at=T0,
    )
    assert mappers.schedule_from_row(mappers.schedule_to_row(news)) == news
    assert mappers.schedule_run_from_row(mappers.schedule_run_to_row(run)) == run
