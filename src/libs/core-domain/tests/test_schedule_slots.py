"""Due times of a schedule (ADR-0039 S5)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from enikk_contracts.schedules import Timing
from enikk_core_domain.schedules import due_slots
from enikk_core_domain.testing import schedule_spec

SEOUL_7AM_IN_UTC = datetime(2026, 11, 24, 22, 0, tzinfo=UTC)  # 11-25 07:00 KST


def test_a_daily_time_is_read_in_its_time_zone() -> None:
    spec = schedule_spec(cron="0 7 * * *")
    after = SEOUL_7AM_IN_UTC - timedelta(hours=1)
    assert due_slots(spec, after, SEOUL_7AM_IN_UTC) == [SEOUL_7AM_IN_UTC]
    assert due_slots(spec, SEOUL_7AM_IN_UTC, SEOUL_7AM_IN_UTC + timedelta(hours=23)) == []


def test_missed_times_are_kept_up_to_catchup() -> None:
    spec = schedule_spec(cron="0 7 * * *")
    after = SEOUL_7AM_IN_UTC - timedelta(days=5, hours=1)
    assert due_slots(spec, after, SEOUL_7AM_IN_UTC) == [SEOUL_7AM_IN_UTC]
    three = spec.model_copy(update={"catchup": 2})
    assert due_slots(three, after, SEOUL_7AM_IN_UTC) == [
        SEOUL_7AM_IN_UTC - timedelta(days=2),
        SEOUL_7AM_IN_UTC - timedelta(days=1),
        SEOUL_7AM_IN_UTC,
    ]


def test_weekdays_and_a_single_time() -> None:
    # 11-25-2026 is a Wednesday (3) in Seoul.
    weekly = schedule_spec(cron="0 7 * * 3")
    after = SEOUL_7AM_IN_UTC - timedelta(days=6)
    assert due_slots(weekly, after, SEOUL_7AM_IN_UTC + timedelta(days=1)) == [SEOUL_7AM_IN_UTC]
    once = schedule_spec().model_copy(update={"timing": Timing(at=SEOUL_7AM_IN_UTC), "catchup": 0})
    assert due_slots(once, after, SEOUL_7AM_IN_UTC) == [SEOUL_7AM_IN_UTC]
    assert due_slots(once, SEOUL_7AM_IN_UTC, SEOUL_7AM_IN_UTC + timedelta(days=1)) == []
