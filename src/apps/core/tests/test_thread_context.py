"""The thread's agent sees what delegates posted in its thread (ADR-0027)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest

from enikk_contracts.common import ConnectorKind, ConversationRef, Engine
from enikk_contracts.events import InboundEvent
from enikk_contracts.outbox import OutboundKind
from enikk_contracts.tasks import DelegateRequest, LeaseRequest, ResultReport, Succeeded
from enikk_core_domain.model import OutboxMessage
from enikk_core_domain.testing import agent
from enikk_core_domain.thread_context import HEADER, MAX_INPUT, with_thread_context

if TYPE_CHECKING:
    from conftest import Harness

    from enikk_contracts.tasks import TaskAssignment

pytestmark = pytest.mark.anyio

NOW = LeaseRequest(wait_seconds=0)
THREAD = ConversationRef(connector=ConnectorKind.SLACK, channel="C1", thread="1800.1")
DRAFT = "PR 은 만들지 않았습니다. ADR-0026 초안 요지: 배포 알림을 #release 로."


@pytest.fixture(autouse=True)
def _assistant_delegates(harness: Harness) -> None:
    base = agent("assistant", Engine.CLAUDE)
    harness.catalog.put(base.model_copy(update={"delegates": ("developer",)}))


async def _say(harness: Harness, event_id: str, text: str) -> TaskAssignment:
    async with harness.client("slack") as slack, harness.client("claude") as claude:
        await slack.submit_event(
            InboundEvent(
                event_id=event_id, conversation=THREAD, user="U1", text=text, agent="assistant"
            )
        )
        lease = (await claude.lease_task(NOW)).assignment
    assert lease is not None
    return lease


async def test_the_next_turn_sees_what_the_delegate_posted(harness: Harness) -> None:
    turn = await _say(harness, "Ev1", "배포 알림을 채널로 옮겨 줘")
    async with harness.client("claude") as claude, harness.client("dev") as dev:
        accepted = await claude.delegate_task(
            turn.task_id, DelegateRequest(lease_id=turn.lease_id, agent="developer", request="X")
        )
        await claude.report_result(
            turn.task_id, ResultReport(lease_id=turn.lease_id, outcome=Succeeded(text="넘겼어요"))
        )
        harness.clock.advance(timedelta(minutes=1))
        await harness.dispatcher.dispatch_once()
        work = (await dev.claim_task(accepted.task_id)).assignment
        assert work is not None
        await dev.report_result(
            work.task_id, ResultReport(lease_id=work.lease_id, outcome=Succeeded(text=DRAFT))
        )

    harness.clock.advance(timedelta(minutes=1))
    nxt = await _say(harness, "Ev2", "방금 초안대로 만들어 줘")
    assert nxt.input.startswith(HEADER)
    assert DRAFT in nxt.input
    assert "넘겼어요" not in nxt.input  # its own reply is already in its conversation
    assert nxt.input.endswith("방금 초안대로 만들어 줘")


async def test_a_new_thread_gets_the_message_as_is(harness: Harness) -> None:
    first = await _say(harness, "Ev1", "안녕")
    assert first.input == "안녕"


def _posted(text: str, minute: int) -> OutboxMessage:
    import uuid

    return OutboxMessage(
        id=uuid.uuid4(),
        task_id=uuid.uuid4(),
        source_event_id="ev",
        conversation=THREAD,
        kind=OutboundKind.REPLY,
        text=text,
        created_at=datetime(2026, 11, 22, 12, minute, tzinfo=UTC),
    )


def test_the_newest_posts_are_kept_and_the_owner_text_is_never_cut() -> None:
    owner = "x" * 30_000
    posted = [_posted(f"old {'a' * 3_900}", 1), _posted(f"new {'b' * 3_900}", 2)] * 3
    composed = with_thread_context(owner, posted)
    assert len(composed) <= MAX_INPUT
    assert composed.endswith(owner)
    assert "new" in composed
    assert composed.count("12:0") < len(posted)
