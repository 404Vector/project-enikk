"""Isolated execution (ADR-0021): dev agent tasks never reach the shared runtime."""

from __future__ import annotations

import uuid
from datetime import timedelta
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
from enikk_core.dispatcher import DispatchOutcome
from enikk_core_client import CoreForbiddenError, CoreNotFoundError

if TYPE_CHECKING:
    from conftest import Harness

pytestmark = pytest.mark.anyio

NOW = LeaseRequest(wait_seconds=0)


def _event(event_id: str, agent: str = "developer", thread: str | None = None) -> InboundEvent:
    return InboundEvent(
        event_id=event_id,
        conversation=ConversationRef(
            connector=ConnectorKind.SLACK, channel="C1", thread=thread or event_id
        ),
        user="U1",
        text=f"change request {event_id}",
        agent=agent,
    )


async def test_shared_runtime_never_leases_isolated_tasks(harness: Harness) -> None:
    async with harness.client("slack") as slack, harness.client("claude") as claude:
        await slack.submit_event(_event("Dev1"))
        assert (await claude.lease_task(NOW)).assignment is None


async def test_dev_agent_claims_its_task_and_reports(harness: Harness) -> None:
    async with harness.client("slack") as slack, harness.client("dev") as dev:
        accepted = await slack.submit_event(_event("Dev1"))
        assignment = (await dev.claim_task(accepted.task_id)).assignment
        assert assignment is not None
        assert assignment.definition.name == "developer"
        await dev.heartbeat(assignment.task_id, Heartbeat(lease_id=assignment.lease_id))
        report = ResultReport(lease_id=assignment.lease_id, outcome=Succeeded(text="PR #1"))
        result = await dev.report_result(assignment.task_id, report)
        assert result.status is TaskStatus.SUCCEEDED
        batch = await slack.poll_outbox(OutboxPoll(wait_seconds=0))
        assert [m.text for m in batch.messages] == ["PR #1"]


async def test_workers_cannot_cross_execution_boundaries(harness: Harness) -> None:
    async with (
        harness.client("slack") as slack,
        harness.client("dev") as dev,
        harness.client("claude") as claude,
    ):
        isolated = await slack.submit_event(_event("Dev1"))
        shared = await slack.submit_event(_event("Ev1", agent="assistant"))
        with pytest.raises(CoreNotFoundError):
            await dev.claim_task(shared.task_id)
        with pytest.raises(CoreForbiddenError):
            await claude.claim_task(isolated.task_id)
        with pytest.raises(CoreForbiddenError):
            await dev.lease_task(NOW)
        shared_lease = (await claude.lease_task(NOW)).assignment
        isolated_lease = (await dev.claim_task(isolated.task_id)).assignment
        assert shared_lease is not None
        assert isolated_lease is not None
        with pytest.raises(CoreNotFoundError):
            await dev.heartbeat(shared_lease.task_id, Heartbeat(lease_id=shared_lease.lease_id))
        with pytest.raises(CoreNotFoundError):
            await claude.heartbeat(
                isolated_lease.task_id, Heartbeat(lease_id=isolated_lease.lease_id)
            )


async def test_a_task_is_claimed_once(harness: Harness) -> None:
    async with harness.client("slack") as slack, harness.client("dev") as dev:
        accepted = await slack.submit_event(_event("Dev1"))
        await dev.claim_task(accepted.task_id)
        with pytest.raises(CoreNotFoundError):
            await dev.claim_task(accepted.task_id)
        with pytest.raises(CoreNotFoundError):
            await dev.claim_task(uuid.uuid4())


async def test_dispatcher_starts_one_job_at_a_time(harness: Harness) -> None:
    dispatcher, launcher = harness.dispatcher, harness.launcher
    assert await dispatcher.dispatch_once() is DispatchOutcome.IDLE
    async with harness.client("slack") as slack:
        first = await slack.submit_event(_event("Dev1"))
        second = await slack.submit_event(_event("Dev2"))
    assert await dispatcher.dispatch_once() is DispatchOutcome.LAUNCHED
    assert await dispatcher.dispatch_once() is DispatchOutcome.BUSY
    assert launcher.launched == [first.task_id]
    async with harness.client("dev") as dev:
        await dev.claim_task(first.task_id)
    launcher.finish_all()
    assert await dispatcher.dispatch_once() is DispatchOutcome.LAUNCHED
    assert launcher.launched == [first.task_id, second.task_id]


async def test_dispatcher_waits_for_the_template(harness: Harness) -> None:
    harness.launcher.deployed = False
    async with harness.client("slack") as slack:
        await slack.submit_event(_event("Dev1"))
    assert await harness.dispatcher.dispatch_once() is DispatchOutcome.NOT_DEPLOYED
    assert harness.launcher.launched == []


async def test_dispatcher_respects_the_daily_limit(harness: Harness) -> None:
    dispatcher, launcher = harness.dispatcher, harness.launcher
    async with harness.client("slack") as slack, harness.client("dev") as dev:
        for n in range(4):
            accepted = await slack.submit_event(_event(f"Dev{n}"))
            outcome = await dispatcher.dispatch_once()
            launcher.finish_all()
            if n < 3:
                assert outcome is DispatchOutcome.LAUNCHED
                lease = (await dev.claim_task(accepted.task_id)).assignment
                assert lease is not None
                await dev.report_result(
                    lease.task_id,
                    ResultReport(lease_id=lease.lease_id, outcome=Succeeded(text="done")),
                )
            else:
                assert outcome is DispatchOutcome.DAILY_LIMIT
    harness.clock.advance(timedelta(hours=25))
    assert await dispatcher.dispatch_once() is DispatchOutcome.LAUNCHED


async def test_task_without_a_reporting_worker_is_abandoned(harness: Harness) -> None:
    dispatcher, launcher = harness.dispatcher, harness.launcher
    async with harness.client("slack") as slack:
        accepted = await slack.submit_event(_event("Dev1"))
        for _ in range(2):  # max_attempts in the harness
            assert await dispatcher.dispatch_once() is DispatchOutcome.LAUNCHED
            launcher.finish_all()
        assert await dispatcher.dispatch_once() is DispatchOutcome.ABANDONED
        batch = await slack.poll_outbox(OutboxPoll(wait_seconds=0))
    assert [m.kind for m in batch.messages] == [OutboundKind.FAILURE]
    assert batch.messages[0].task_id == accepted.task_id
    assert await dispatcher.dispatch_once() is DispatchOutcome.IDLE
