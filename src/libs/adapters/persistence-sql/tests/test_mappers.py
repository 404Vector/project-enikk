from __future__ import annotations

import uuid
from datetime import timedelta

from enikk_core_domain.testing import T0, outbox_message, session, task
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
