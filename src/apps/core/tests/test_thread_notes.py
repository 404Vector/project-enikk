"""Agent PR progress in the thread that asked for it (ADR-0025)."""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

import pytest

from enikk_contracts.approvals import PullStage, PullStatusCard
from enikk_contracts.common import ConnectorKind
from enikk_contracts.outbox import OutboundKind
from enikk_contracts.threads import ReviewThreadOpened
from enikk_core.threads import ReviewLinks, ThreadNotes, branch_owner
from enikk_core_domain.model import Session
from enikk_core_domain.testing import T0, agent, conversation, task
from enikk_core_domain.testing import session as thread_session
from enikk_kernel.ids import RandomIdGenerator

if TYPE_CHECKING:
    from conftest import Harness

pytestmark = pytest.mark.anyio


async def _delegated(harness: Harness) -> tuple[Session, uuid.UUID]:
    origin = conversation("1.0")
    session = Session.start(
        session_id=uuid.uuid4(),
        conversation=conversation("1.0:developer"),
        agent=agent("developer"),
        now=T0,
        reply_to=origin,
    )
    work = task(session)
    async with harness.db.unit_of_work() as uow:
        await uow.sessions.add(session)
        await uow.tasks.add(work)
        await uow.commit()
    return session, work.id


async def _own_thread(harness: Harness) -> Session:
    """A Slack thread's own session, whose commits core pushed (ADR-0037 §6b)."""
    own = thread_session(thread="2.0")
    async with harness.db.unit_of_work() as uow:
        await uow.sessions.add(own)
        await uow.tasks.add(task(own))
        await uow.commit()
    return own


def test_branch_names_lead_to_their_owner() -> None:
    owner = uuid.uuid4()
    assert branch_owner(f"agent/{owner}") == owner
    assert branch_owner("agent/revert-abc") is None
    assert branch_owner("feat/x") is None


@pytest.mark.parametrize("by_task", [False, True])
async def test_note_goes_to_the_delegating_thread(harness: Harness, by_task: bool) -> None:
    session, task_id = await _delegated(harness)
    notes = ThreadNotes(harness.clock, RandomIdGenerator())
    branch = f"agent/{task_id if by_task else session.id}"
    async with harness.db.unit_of_work() as uow:
        assert await notes.post(uow, branch, "PR #7 이 머지됐습니다.")
        await uow.commit()
    async with harness.db.unit_of_work() as uow:
        [message] = await uow.outbox.pending(ConnectorKind.SLACK, 10)
    assert message.conversation == conversation("1.0")
    assert message.kind is OutboundKind.REPLY
    assert message.task_id == task_id
    assert message.text == "PR #7 이 머지됐습니다."


@pytest.mark.parametrize("delegated", [True, False])
async def test_the_status_card_goes_to_the_thread_that_asked(
    harness: Harness, delegated: bool
) -> None:
    if delegated:
        owner, _ = await _delegated(harness)
        expected = conversation("1.0")
    else:
        owner = await _own_thread(harness)
        expected = conversation("2.0")
    notes = ThreadNotes(harness.clock, RandomIdGenerator())
    async with harness.db.unit_of_work() as uow:
        assert await notes.card(
            uow,
            f"agent/{owner.id}",
            repository="404Vector/project-enikk",
            number=7,
            url="https://github.com/404Vector/project-enikk/pull/7",
            title="docs: x",
            stage=PullStage.CHECKING,
        )
        await uow.commit()
    async with harness.db.unit_of_work() as uow:
        [notice] = await uow.notices.pending(10)
    assert isinstance(notice.body, PullStatusCard)
    assert notice.body.origin == expected


async def test_the_review_thread_links_back_to_a_thread_s_own_pull_request(
    harness: Harness,
) -> None:
    own = await _own_thread(harness)
    links = ReviewLinks(harness.db.unit_of_work, ThreadNotes(harness.clock, RandomIdGenerator()))
    linked = await links.opened(
        ReviewThreadOpened(repository="404Vector/project-enikk", number=7, branch=f"agent/{own.id}")
    )
    assert linked.origin == conversation("2.0")


async def test_branches_without_a_thread_are_skipped(harness: Harness) -> None:
    notes = ThreadNotes(harness.clock, RandomIdGenerator())
    async with harness.db.unit_of_work() as uow:
        assert not await notes.post(uow, f"agent/{uuid.uuid4()}", "x")
        assert not await notes.post(uow, "feat/owner", "x")
