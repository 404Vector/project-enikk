"""Delegation (ADR-0022): the assistant hands a request to the developer agent."""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

import pytest
from pydantic import ValidationError

from enikk_contracts.agents import Execution
from enikk_contracts.common import ConnectorKind, ConversationRef, Engine
from enikk_contracts.events import InboundEvent
from enikk_contracts.outbox import OutboxAck, OutboxPoll
from enikk_contracts.tasks import (
    DelegateRequest,
    LeaseRequest,
    ResultReport,
    Succeeded,
    TaskAssignment,
)
from enikk_contracts.threads import ReviewThreadLinked, ReviewThreadOpened
from enikk_core.dispatcher import DispatchOutcome
from enikk_core_client import (
    CoreClient,
    CoreConflictError,
    CoreForbiddenError,
    CoreNotFoundError,
)
from enikk_core_domain.testing import agent

if TYPE_CHECKING:
    from conftest import Harness

pytestmark = pytest.mark.anyio

NOW = LeaseRequest(wait_seconds=0)
THREAD = ConversationRef(connector=ConnectorKind.SLACK, channel="C1", thread="1700.1")


@pytest.fixture(autouse=True)
def _assistant_delegates(harness: Harness) -> None:
    base = agent("assistant", Engine.CLAUDE)
    harness.catalog.put(base.model_copy(update={"delegates": ("developer",)}))


async def _assistant_turn(harness: Harness, event_id: str = "Ev1") -> TaskAssignment:
    async with harness.client("slack") as slack, harness.client("claude") as claude:
        await slack.submit_event(
            InboundEvent(
                event_id=event_id,
                conversation=THREAD,
                user="U1",
                text="please change the README",
                agent="assistant",
            )
        )
        lease = (await claude.lease_task(NOW)).assignment
    assert lease is not None
    return lease


async def _finish(client: CoreClient, lease: TaskAssignment, text: str) -> None:
    await client.report_result(
        lease.task_id, ResultReport(lease_id=lease.lease_id, outcome=Succeeded(text=text))
    )


async def test_delegated_work_answers_in_the_original_thread(harness: Harness) -> None:
    turn = await _assistant_turn(harness)
    async with (
        harness.client("claude") as claude,
        harness.client("dev") as dev,
        harness.client("slack") as slack,
    ):
        request = DelegateRequest(lease_id=turn.lease_id, agent="developer", request="Do X")
        accepted = await claude.delegate_task(turn.task_id, request)
        again = await claude.delegate_task(turn.task_id, request)
        assert not accepted.duplicate
        assert again.duplicate
        assert again.task_id == accepted.task_id
        await _finish(claude, turn, "개발 에이전트에 넘겼습니다.")

        assert await harness.dispatcher.dispatch_once() is DispatchOutcome.LAUNCHED
        work = (await dev.claim_task(accepted.task_id)).assignment
        assert work is not None
        assert work.input == "Do X"
        assert work.definition.execution is Execution.ISOLATED
        await _finish(dev, work, "PR 을 만들지 않았습니다: 요청이 모호합니다.")

        batch = await slack.poll_outbox(OutboxPoll(wait_seconds=0))
    assert [m.text for m in batch.messages] == [
        "개발 에이전트에 넘겼습니다.",
        "PR 을 만들지 않았습니다: 요청이 모호합니다.",
    ]
    assert all(m.conversation == THREAD for m in batch.messages)


REPORT = (
    "PR: https://github.com/404Vector/project-enikk/pull/7\n"
    "바뀐 것: `src/secret_plan.py` 에 함수 추가\n```diff\n+def x(): ...\n```"
)


async def test_the_asking_thread_sees_status_and_the_review_thread_the_report(
    harness: Harness,
) -> None:
    turn = await _assistant_turn(harness)
    async with (
        harness.client("claude") as claude,
        harness.client("dev") as dev,
        harness.client("slack") as slack,
    ):
        accepted = await claude.delegate_task(
            turn.task_id, DelegateRequest(lease_id=turn.lease_id, agent="developer", request="X")
        )
        await _finish(claude, turn, "넘겼습니다.")
        await harness.dispatcher.dispatch_once()
        work = (await dev.claim_task(accepted.task_id)).assignment
        assert work is not None
        await _finish(dev, work, REPORT)
        [handed, status] = (await slack.poll_outbox(OutboxPoll(wait_seconds=0))).messages
        assert status.conversation == THREAD
        assert "https://github.com/404Vector/project-enikk/pull/7" in status.text
        assert "secret_plan" not in status.text
        assert "diff" not in status.text
        await slack.ack_outbox(OutboxAck(message_ids=(handed.message_id, status.message_id)))

        linked = await slack.review_thread_opened(
            ReviewThreadOpened(
                repository="404Vector/project-enikk",
                number=7,
                branch=f"agent/{work.session_id}",
                review_link="https://slack.test/archives/D1/p1",
            )
        )
        assert linked.origin == THREAD
        assert linked.report == REPORT
        # The asking thread links to the approval message through its status card
        # (ADR-0036), not with a separate message.
        assert not (await slack.poll_outbox(OutboxPoll(wait_seconds=0))).messages


async def test_review_threads_of_other_branches_link_nothing(harness: Harness) -> None:
    async with harness.client("slack") as slack, harness.client("claude") as claude:
        review = ReviewThreadOpened(repository="o/r", number=3, branch="feat/owner")
        assert await slack.review_thread_opened(review) == ReviewThreadLinked()
        assert not (await slack.poll_outbox(OutboxPoll(wait_seconds=0))).messages
        with pytest.raises(CoreForbiddenError):
            await claude.review_thread_opened(review)


async def test_thread_messages_keep_going_to_the_assistant(harness: Harness) -> None:
    turn = await _assistant_turn(harness)
    async with harness.client("claude") as claude:
        await claude.delegate_task(
            turn.task_id, DelegateRequest(lease_id=turn.lease_id, agent="developer", request="X")
        )
        await _finish(claude, turn, "ok")
    follow_up = await _assistant_turn(harness, "Ev2")
    assert follow_up.definition.name == "assistant"
    async with harness.client("claude") as claude, harness.client("dev") as dev:
        second = await claude.delegate_task(
            follow_up.task_id,
            DelegateRequest(lease_id=follow_up.lease_id, agent="developer", request="Also Y"),
        )
        # Both delegations share the thread's developer session: the second waits
        # behind the first (I-29).
        with pytest.raises(CoreNotFoundError):
            await dev.claim_task(second.task_id)


async def test_only_declared_delegates_and_live_leases(harness: Harness) -> None:
    turn = await _assistant_turn(harness)
    async with harness.client("claude") as claude:
        with pytest.raises(CoreForbiddenError, match="may not delegate"):
            await claude.delegate_task(
                turn.task_id,
                DelegateRequest(lease_id=turn.lease_id, agent="painter", request="X"),
            )
        await _finish(claude, turn, "done")
        with pytest.raises(CoreConflictError):
            await claude.delegate_task(
                turn.task_id,
                DelegateRequest(lease_id=turn.lease_id, agent="developer", request="X"),
            )


async def test_delegation_stops_at_the_daily_limit(harness: Harness) -> None:
    for n in range(3):  # daily_limit in the harness
        turn = await _assistant_turn(harness, f"Ev{n}")
        async with harness.client("claude") as claude, harness.client("dev") as dev:
            accepted = await claude.delegate_task(
                turn.task_id,
                DelegateRequest(lease_id=turn.lease_id, agent="developer", request=f"X{n}"),
            )
            await _finish(claude, turn, "ok")
            assert await harness.dispatcher.dispatch_once() is DispatchOutcome.LAUNCHED
            harness.launcher.finish_all()
            work = (await dev.claim_task(accepted.task_id)).assignment
            assert work is not None
            await _finish(dev, work, "done")
    turn = await _assistant_turn(harness, "Ev9")
    async with harness.client("claude") as claude:
        with pytest.raises(CoreConflictError, match="daily limit"):
            await claude.delegate_task(
                turn.task_id,
                DelegateRequest(lease_id=turn.lease_id, agent="developer", request="X"),
            )


def test_requests_are_bounded() -> None:
    with pytest.raises(ValidationError, match="at most 20000"):
        DelegateRequest(lease_id=uuid.uuid4(), agent="developer", request="x" * 20_001)
