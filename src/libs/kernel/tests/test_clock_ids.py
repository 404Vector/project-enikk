from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from enikk_kernel.clock import SystemClock
from enikk_kernel.ids import RandomIdGenerator
from enikk_kernel.testing import ManualClock, SequentialIds


def test_system_clock_is_timezone_aware() -> None:
    assert SystemClock().now().tzinfo is UTC


def test_manual_clock_moves_only_when_advanced() -> None:
    clock = ManualClock(datetime(2026, 5, 1, tzinfo=UTC))
    first = clock.now()
    assert clock.now() == first
    clock.advance(timedelta(seconds=30))
    assert clock.now() == first + timedelta(seconds=30)


def test_manual_clock_rejects_naive_start() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        ManualClock(datetime(2026, 5, 1))


def test_sequential_ids_are_predictable() -> None:
    ids = SequentialIds()
    assert [ids.new(), ids.new()] == [uuid.UUID(int=1), uuid.UUID(int=2)]


def test_random_ids_are_unique() -> None:
    generator = RandomIdGenerator()
    assert generator.new() != generator.new()
