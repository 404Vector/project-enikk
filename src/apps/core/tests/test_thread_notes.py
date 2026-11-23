"""Agent PR progress in the thread that asked for it (ADR-0025)."""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

import pytest

from enikk_contracts.common import ConnectorKind
from enikk_contracts.outbox import OutboundKind
from enikk_core.threads import ThreadNotes, branch_owner, delegated_status
from enikk_core_domain.model import Session
from enikk_core_domain.testing import T0, agent, conversation, task
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


async def test_branches_without_a_thread_are_skipped(harness: Harness) -> None:
    notes = ThreadNotes(harness.clock, RandomIdGenerator())
    async with harness.db.unit_of_work() as uow:
        assert not await notes.post(uow, f"agent/{uuid.uuid4()}", "x")
        assert not await notes.post(uow, "feat/owner", "x")


def test_a_delegated_answer_with_a_pull_request_becomes_a_status_line() -> None:
    url = "https://github.com/404Vector/project-enikk/pull/7"
    report = f"PR 을 만들었습니다: {url}\n- src/a.py 수정\n```py\nx = 1\n```\n다시 {url}"
    status = delegated_status("developer", report)
    assert status.count(url) == 1
    assert "src/a.py" not in status
    assert "x = 1" not in status
    assert "소유자 DM" in status
    assert status.startswith("에닉의 developer 역할이 작업을 마쳤습니다.")  # ADR-0031


def test_a_delegated_answer_without_a_pull_request_is_kept() -> None:
    assert delegated_status("developer", "모호해서 PR 을 만들지 않았습니다.") == (
        "모호해서 PR 을 만들지 않았습니다."
    )
