"""The owner cancels a thread's work from Slack (ADR-0037 3b)."""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

import pytest

from enikk_contracts.common import ConnectorKind, ConversationRef
from enikk_contracts.events import InboundEvent
from enikk_contracts.outbox import OutboundKind, OutboxPoll
from enikk_contracts.tasks import (
    Heartbeat,
    LeaseRequest,
    ResultReport,
    Succeeded,
    TaskStatus,
)
from enikk_core.usecases import CANCELLED_TEXT, STARTED_TEXT
from enikk_core_client import CoreConflictError, CoreForbiddenError, CoreNotFoundError

if TYPE_CHECKING:
    from conftest import Harness

pytestmark = pytest.mark.anyio

NOW = LeaseRequest(wait_seconds=0)
THREAD = ConversationRef(connector=ConnectorKind.SLACK, channel="C1", thread="1800.1")
POLL = OutboxPoll(wait_seconds=0)


async def _submit(harness: Harness, event_id: str, thread: str = "1800.1") -> uuid.UUID:
    conversation = THREAD.model_copy(update={"thread": thread})
    async with harness.client("slack") as slack:
        accepted = await slack.submit_event(
            InboundEvent(
                event_id=event_id,
                conversation=conversation,
                user="U1",
                text="hi",
                agent="assistant",
            )
        )
    return accepted.task_id


async def _status(harness: Harness, task_id: uuid.UUID) -> TaskStatus:
    async with harness.db.unit_of_work() as uow:
        task = await uow.tasks.get_for_update(task_id)
    assert task is not None
    return task.status


async def test_a_running_task_is_cancelled_and_its_worker_is_refused(harness: Harness) -> None:
    task_id = await _submit(harness, "slack:C1:1800.1")
    async with harness.client("claude") as claude, harness.client("slack") as slack:
        lease = (await claude.lease_task(NOW)).assignment
        assert lease is not None
        result = await slack.cancel_task(task_id)
        assert result.cancelled == (task_id,)
        with pytest.raises(CoreConflictError):
            await claude.heartbeat(task_id, Heartbeat(lease_id=lease.lease_id))
        with pytest.raises(CoreConflictError):
            await claude.report_result(
                task_id, ResultReport(lease_id=lease.lease_id, outcome=Succeeded(text="late"))
            )
        messages = (await slack.poll_outbox(POLL)).messages
    assert await _status(harness, task_id) is TaskStatus.CANCELLED
    assert [(m.kind, m.text) for m in messages] == [
        (OutboundKind.STARTED, STARTED_TEXT),
        (OutboundKind.CANCELLED, CANCELLED_TEXT),
    ]


async def test_queued_tasks_of_the_thread_are_cancelled_too(harness: Harness) -> None:
    first = await _submit(harness, "slack:C1:1800.1")
    second = await _submit(harness, "slack:C1:1800.2")
    other = await _submit(harness, "slack:C1:1900.1", thread="1900.1")
    async with harness.client("slack") as slack:
        result = await slack.cancel_task(first)
    assert set(result.cancelled) == {first, second}
    assert await _status(harness, second) is TaskStatus.CANCELLED
    assert await _status(harness, other) is TaskStatus.QUEUED


async def test_a_finished_task_is_left_alone(harness: Harness) -> None:
    task_id = await _submit(harness, "slack:C1:1800.1")
    async with harness.client("claude") as claude, harness.client("slack") as slack:
        lease = (await claude.lease_task(NOW)).assignment
        assert lease is not None
        await claude.report_result(
            task_id, ResultReport(lease_id=lease.lease_id, outcome=Succeeded(text="done"))
        )
        result = await slack.cancel_task(task_id)
        kinds = [m.kind for m in (await slack.poll_outbox(POLL)).messages]
    assert result.cancelled == ()
    assert await _status(harness, task_id) is TaskStatus.SUCCEEDED
    assert kinds == [OutboundKind.STARTED, OutboundKind.REPLY]


async def test_only_the_slack_connector_cancels(harness: Harness) -> None:
    task_id = await _submit(harness, "slack:C1:1800.1")
    async with harness.client("claude") as claude, harness.client("smoke") as smoke:
        with pytest.raises(CoreForbiddenError):
            await claude.cancel_task(task_id)
        with pytest.raises(CoreForbiddenError):
            await smoke.cancel_task(task_id)
    assert await _status(harness, task_id) is TaskStatus.QUEUED


async def test_an_unknown_task_is_not_found(harness: Harness) -> None:
    async with harness.client("slack") as slack:
        with pytest.raises(CoreNotFoundError):
            await slack.cancel_task(uuid.uuid4())
