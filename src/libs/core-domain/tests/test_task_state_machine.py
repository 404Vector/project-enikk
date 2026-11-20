from __future__ import annotations

import uuid
from datetime import timedelta
from typing import TYPE_CHECKING

import pytest
from pydantic import ValidationError

from enikk_contracts.tasks import TaskStatus
from enikk_core_domain.errors import InvalidTransitionError, LeaseLostError
from enikk_core_domain.testing import T0, session, task

if TYPE_CHECKING:
    from enikk_core_domain.model import Task

MINUTE = timedelta(minutes=1)


def _leased(lease_id: uuid.UUID | None = None) -> Task:
    return task(session()).grant_lease(lease_id=lease_id or uuid.uuid4(), now=T0, duration=MINUTE)


def test_new_task_is_queued() -> None:
    queued = task(session())
    assert queued.status is TaskStatus.QUEUED
    assert queued.attempt == 0
    assert queued.lease is None


def test_grant_lease_counts_attempts() -> None:
    lease_id = uuid.uuid4()
    leased = _leased(lease_id)
    assert leased.status is TaskStatus.LEASED
    assert leased.attempt == 1
    assert leased.lease is not None
    assert leased.lease.id == lease_id
    assert leased.lease.expires_at == T0 + MINUTE


def test_cannot_lease_twice() -> None:
    with pytest.raises(InvalidTransitionError):
        _leased().grant_lease(lease_id=uuid.uuid4(), now=T0, duration=MINUTE)


def test_extend_lease_and_record_progress() -> None:
    lease_id = uuid.uuid4()
    later = T0 + timedelta(seconds=30)
    extended = _leased(lease_id).extend_lease(
        lease_id=lease_id, now=later, duration=MINUTE, progress="thinking"
    )
    assert extended.lease is not None
    assert extended.lease.expires_at == later + MINUTE
    assert extended.progress == "thinking"


def test_wrong_or_expired_lease_is_rejected() -> None:
    lease_id = uuid.uuid4()
    leased = _leased(lease_id)
    with pytest.raises(LeaseLostError):
        leased.succeed(lease_id=uuid.uuid4(), text="x", now=T0)
    with pytest.raises(LeaseLostError):
        leased.succeed(lease_id=lease_id, text="x", now=T0 + MINUTE)


def test_succeed_records_output_and_detects_duplicates() -> None:
    lease_id = uuid.uuid4()
    leased = _leased(lease_id)
    assert not leased.already_reported(lease_id)
    done = leased.succeed(lease_id=lease_id, text="answer", now=T0)
    assert done.status is TaskStatus.SUCCEEDED
    assert done.output == "answer"
    assert done.terminal
    assert done.already_reported(lease_id)
    assert not done.already_reported(uuid.uuid4())


def test_retryable_failure_requeues_until_attempts_run_out() -> None:
    current = task(session())
    for attempt in range(1, current.max_attempts + 1):
        lease_id = uuid.uuid4()
        current = current.grant_lease(lease_id=lease_id, now=T0, duration=MINUTE)
        assert current.attempt == attempt
        current = current.fail(lease_id=lease_id, reason="flaky", retryable=True, now=T0)
    assert current.status is TaskStatus.FAILED
    assert current.failure == "flaky"


def test_non_retryable_failure_is_final() -> None:
    lease_id = uuid.uuid4()
    failed = _leased(lease_id).fail(lease_id=lease_id, reason="bad", retryable=False, now=T0)
    assert failed.status is TaskStatus.FAILED


def test_expire_requeues_then_fails() -> None:
    current = task(session())
    for _ in range(current.max_attempts):
        current = current.grant_lease(lease_id=uuid.uuid4(), now=T0, duration=MINUTE)
        with pytest.raises(InvalidTransitionError):
            current.expire(T0)
        current = current.expire(T0 + MINUTE)
    assert current.status is TaskStatus.FAILED
    assert current.failure is not None


def test_cancel() -> None:
    cancelled = _leased().cancel(T0)
    assert cancelled.status is TaskStatus.CANCELLED
    with pytest.raises(InvalidTransitionError):
        cancelled.cancel(T0)


def test_inconsistent_state_cannot_be_constructed() -> None:
    queued = task(session())
    with pytest.raises(ValidationError, match="lease exactly when"):
        type(queued)(**{**dict(queued), "status": TaskStatus.LEASED})
    with pytest.raises(ValidationError, match="output exactly when"):
        type(queued)(**{**dict(queued), "status": TaskStatus.SUCCEEDED})


def test_session_binds_engine_session() -> None:
    owner = session()
    bound = owner.bind_engine_session("abc", T0 + MINUTE)
    assert bound.engine_session_id == "abc"
    assert bound.updated_at == T0 + MINUTE
    assert bound.bind_engine_session("abc", T0 + 2 * MINUTE) is bound
