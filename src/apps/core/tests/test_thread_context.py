"""The thread's agent sees what other sessions posted in its thread (ADR-0027)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest

from enikk_contracts.agents import Execution
from enikk_contracts.common import ConnectorKind, ConversationRef
from enikk_contracts.events import InboundEvent
from enikk_contracts.outbox import OutboundKind
from enikk_contracts.tasks import LeaseRequest, ResultReport, Succeeded
from enikk_core_domain.model import OutboxMessage, Session
from enikk_core_domain.testing import agent, task
from enikk_core_domain.thread_context import HEADER, MAX_INPUT, with_thread_context

if TYPE_CHECKING:
    from conftest import Harness

    from enikk_contracts.tasks import TaskAssignment

pytestmark = pytest.mark.anyio

NOW = LeaseRequest(wait_seconds=0)
THREAD = ConversationRef(connector=ConnectorKind.SLACK, channel="C1", thread="1800.1")
DRAFT = "PR 은 만들지 않았습니다. ADR-0026 초안 요지: 배포 알림을 #release 로."


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


async def _posted_by_another_session(harness: Harness, text: str) -> None:
    """A message another session (an earlier delegate) posted into ``THREAD``."""
    import uuid

    now = harness.clock.now()
    other = Session.start(
        session_id=uuid.uuid4(),
        conversation=THREAD.model_copy(update={"thread": f"{THREAD.thread}:developer"}),
        agent=agent("developer"),
        now=now,
        reply_to=THREAD,
    )
    work = task(other, now=now, execution=Execution.ISOLATED)
    async with harness.db.unit_of_work() as uow:
        await uow.sessions.add(other)
        await uow.tasks.add(work)
        await uow.outbox.add(
            OutboxMessage(
                id=uuid.uuid4(),
                task_id=work.id,
                source_event_id="ev-other",
                conversation=THREAD,
                kind=OutboundKind.REPLY,
                text=text,
                created_at=now,
            )
        )
        await uow.commit()


async def test_the_next_turn_sees_what_another_session_posted(harness: Harness) -> None:
    turn = await _say(harness, "Ev1", "배포 알림을 채널로 옮겨 줘")
    async with harness.client("claude") as claude:
        await claude.report_result(
            turn.task_id, ResultReport(lease_id=turn.lease_id, outcome=Succeeded(text="넘겼어요"))
        )
    harness.clock.advance(timedelta(minutes=1))
    await _posted_by_another_session(harness, DRAFT)

    harness.clock.advance(timedelta(minutes=1))
    nxt = await _say(harness, "Ev2", "방금 초안대로 만들어 줘")
    assert nxt.input.startswith(HEADER)
    # The assistant's prompt quotes this header (ADR-0031 wording).
    assert HEADER == "[이 스레드에 그 사이 에닉의 다른 역할과 시스템이 올린 내용]"
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
