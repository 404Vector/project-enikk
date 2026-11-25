"""A thread's session wakes when its PR or prompt candidate moves on (ADR-0041 §4)."""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import TYPE_CHECKING

import pytest

from enikk_contracts.approvals import PullStage
from enikk_contracts.prompts import PromotionStatus
from enikk_core.threads import ThreadNotes
from enikk_core.wakeups import (
    EVENT_PREFIX,
    HEADER,
    SessionWaker,
    promotion_event_text,
    pull_event_text,
)
from enikk_core_domain.model import Session
from enikk_core_domain.testing import T0, agent, conversation, task
from enikk_core_domain.testing import session as thread_session
from enikk_kernel.ids import RandomIdGenerator

if TYPE_CHECKING:
    from conftest import Harness

pytestmark = pytest.mark.anyio

REPO = "404Vector/project-enikk"


def _waker(harness: Harness) -> SessionWaker:
    return SessionWaker(harness.catalog, harness.clock, RandomIdGenerator(), max_attempts=2)


async def _own_thread(harness: Harness) -> Session:
    own = thread_session(thread="2.0")
    async with harness.db.unit_of_work() as uow:
        await uow.sessions.add(own)
        await uow.tasks.add(task(own))
        await uow.commit()
    return own


async def _card(notes: ThreadNotes, harness: Harness, branch: str, stage: PullStage) -> None:
    async with harness.db.unit_of_work() as uow:
        assert await notes.card(
            uow,
            branch,
            repository=REPO,
            number=7,
            url=f"https://github.com/{REPO}/pull/7",
            title="feat: x",
            stage=stage,
            commit="abcdef1234567890abcdef1234567890abcdef12",
        )
        await uow.commit()


async def test_a_merge_wakes_the_thread_once(harness: Harness) -> None:
    own = await _own_thread(harness)
    notes = ThreadNotes(harness.clock, RandomIdGenerator(), _waker(harness))
    harness.clock.advance(timedelta(minutes=1))
    await _card(notes, harness, f"agent/{own.id}", PullStage.MERGED)
    await _card(notes, harness, f"agent/{own.id}", PullStage.MERGED)
    async with harness.db.unit_of_work() as uow:
        assert len(await uow.tasks.active_in_session(own.id)) == 2
        woken = await uow.tasks.latest_in_session(own.id)
        event = await uow.tasks.find_by_source_event(
            f"{EVENT_PREFIX}pr:{REPO}#7:merged:abcdef1234567890abcdef1234567890abcdef12"
        )
    assert woken is not None
    assert event is not None
    assert woken.id == event.id
    assert woken.input.startswith(f"{HEADER} PR #7 머지됨")
    assert "abcdef123456" in woken.input
    assert woken.max_attempts == 2


@pytest.mark.parametrize("stage", [PullStage.CHECKING, PullStage.AWAITING_APPROVAL])
async def test_stages_waiting_for_the_owner_do_not_wake(harness: Harness, stage: PullStage) -> None:
    own = await _own_thread(harness)
    notes = ThreadNotes(harness.clock, RandomIdGenerator(), _waker(harness))
    async with harness.db.unit_of_work() as uow:
        before = await uow.tasks.latest_in_session(own.id)
    await _card(notes, harness, f"agent/{own.id}", stage)
    async with harness.db.unit_of_work() as uow:
        after = await uow.tasks.latest_in_session(own.id)
    assert before == after


async def test_without_a_waker_only_the_card_goes(harness: Harness) -> None:
    own = await _own_thread(harness)
    notes = ThreadNotes(harness.clock, RandomIdGenerator())
    await _card(notes, harness, f"agent/{own.id}", PullStage.DEPLOYED)
    async with harness.db.unit_of_work() as uow:
        assert len(await uow.notices.pending(10)) == 1
        latest = await uow.tasks.latest_in_session(own.id)
    assert latest is not None
    assert not latest.source_event_id.startswith(EVENT_PREFIX)


async def test_a_delegated_session_is_not_woken(harness: Harness) -> None:
    delegated = Session.start(
        session_id=uuid.uuid4(),
        conversation=conversation("1.0:developer"),
        agent=agent("developer"),
        now=T0,
        reply_to=conversation("1.0"),
    )
    async with harness.db.unit_of_work() as uow:
        await uow.sessions.add(delegated)
        await uow.tasks.add(task(delegated))
        assert not await _waker(harness).wake(uow, delegated, key="k", text="x")


async def test_an_agent_that_is_gone_is_not_woken(harness: Harness) -> None:
    gone = Session.start(
        session_id=uuid.uuid4(), conversation=conversation("3.0"), agent=agent("gone"), now=T0
    )
    async with harness.db.unit_of_work() as uow:
        await uow.sessions.add(gone)
        assert not await _waker(harness).wake(uow, gone, key="k", text="x")


def test_event_texts_say_what_happened_and_that_core_sent_them() -> None:
    text = pull_event_text(
        repository=REPO,
        number=9,
        title="fix: y",
        stage=PullStage.CHECKS_FAILED,
        commit=None,
        detail="실패한 검사: checks",
    )
    assert text.startswith(f"{HEADER} PR #9 검사 실패")
    assert "실패한 검사: checks" in text
    assert "소유자의 메시지가 아닙니다" in text
    promoted = promotion_event_text("enikk", 3, PromotionStatus.PROMOTED, "반영: 다음 작업부터")
    assert promoted.startswith(f"{HEADER} 프롬프트 `enikk` v3 production 으로 승격됨")
