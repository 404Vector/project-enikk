"""Slack approval of pull requests (ADR-0020): watch, ask, decide."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import TYPE_CHECKING

import pytest

from enikk_contracts.approvals import (
    ApprovalRequested,
    ApprovalResolved,
    ChecksFailed,
    Decision,
    DecisionRequest,
    NoticeAck,
    NoticePoll,
    OwnerNotice,
    PullStage,
    PullStatusCard,
    ResolutionOutcome,
    StatusCardPosted,
)
from enikk_core.approval.service import ApprovalService
from enikk_core.threads import ThreadNotes
from enikk_core_client import CoreForbiddenError
from enikk_core_domain.model import ApprovalState
from enikk_core_domain.repositories import RepositoryPolicy, SensitivePaths
from enikk_github.permissions import TokenPurpose
from enikk_github.pulls import CheckRun, FileChange, MergeResult, PullInfo
from enikk_kernel.ids import RandomIdGenerator

if TYPE_CHECKING:
    from conftest import Harness

    from enikk_github.app import InstallationToken

pytestmark = pytest.mark.anyio

POLICY = RepositoryPolicy.model_validate_json(
    '{"repositories": [{"name": "project-enikk", "self": true}, {"name": "side-project"}]}'
)
SENSITIVE = SensitivePaths(paths=("docs/adr/", "AGENTS.md"))
SHA1 = "1" * 40
SHA2 = "2" * 40
OWNER = "U04CF12EKRA"
REPO = "404Vector/project-enikk"


def _pull(number: int = 7, sha: str = SHA1, branch: str = "agent/abc") -> PullInfo:
    return PullInfo(
        number=number,
        title="docs: add a line",
        url=f"https://github.com/404Vector/project-enikk/pull/{number}",
        body="원래 요청: README 에 한 줄",
        draft=False,
        open=True,
        merged=False,
        author="enikk-deployer[bot]",
        head_sha=sha,
        branch=branch,
        from_fork=False,
    )


class FakePulls:
    def __init__(self) -> None:
        self.pulls: dict[int, PullInfo] = {}
        self.runs: dict[str, list[CheckRun]] = {}
        self.approved: list[str] = []
        self.merged: list[int] = []
        self.closed: list[int] = []
        self.deleted: list[str] = []
        self.merge_result = MergeResult(merged=True)
        self.purposes: list[TokenPurpose] = []
        self.updated: list[int] = []
        self.changed = [FileChange(path="README.md", additions=1, deletions=0)]

    async def open_pulls(self, token: InstallationToken, base: str = "main") -> list[PullInfo]:
        if token.repository != "project-enikk":
            return []
        return [p for p in self.pulls.values() if p.open]

    async def get(self, token: InstallationToken, number: int) -> PullInfo:
        return self.pulls[number]

    async def check_runs(self, token: InstallationToken, sha: str) -> list[CheckRun]:
        return self.runs.get(sha, [])

    async def files(self, token: InstallationToken, number: int) -> list[FileChange]:
        return list(self.changed)

    async def diff(self, token: InstallationToken, number: int) -> tuple[str, bool]:
        return "+one line", False

    async def approve(self, token: InstallationToken, sha: str) -> None:
        self.approved.append(sha)

    async def merge(self, token: InstallationToken, pull: PullInfo) -> MergeResult:
        if self.merge_result.merged:
            self.merged.append(pull.number)
        return self.merge_result

    async def close(self, token: InstallationToken, number: int) -> None:
        self.closed.append(number)

    async def delete_branch(self, token: InstallationToken, branch: str) -> None:
        self.deleted.append(branch)

    async def update_branch(self, token: InstallationToken, pull: PullInfo) -> bool:
        self.updated.append(pull.number)
        return True


def _passed(*names: str) -> list[CheckRun]:
    return [CheckRun(name=n, completed=True, conclusion="success") for n in names]


@pytest.fixture
def pulls() -> FakePulls:
    return FakePulls()


@pytest.fixture
def service(harness: Harness, pulls: FakePulls) -> ApprovalService:
    async def no_sleep(seconds: float) -> None:
        harness.clock.advance(timedelta(seconds=seconds))

    return ApprovalService(
        uow=harness.db.unit_of_work,
        app=harness.github,
        pulls=pulls,
        policy=POLICY,
        sensitive=SENSITIVE,
        clock=harness.clock,
        ids=RandomIdGenerator(),
        sleep=no_sleep,
        threads=ThreadNotes(harness.clock, RandomIdGenerator()),
    )


async def _notices(service: ApprovalService) -> list[object]:
    batch = await service.poll_notices(NoticePoll(wait_seconds=0))
    if batch.notices:
        await service.ack_notices(NoticeAck(notice_ids=tuple(n.notice_id for n in batch.notices)))
    return [n.body for n in batch.notices]


async def test_owner_is_asked_once_when_checks_pass(
    service: ApprovalService, pulls: FakePulls
) -> None:
    pulls.pulls[7] = _pull()
    pulls.runs[SHA1] = [CheckRun(name="checks", completed=False, conclusion=None)]
    await service.watch_once()
    assert await _notices(service) == []

    pulls.runs[SHA1] = _passed("checks", "protected-paths")
    await service.watch_once()
    await service.watch_once()
    [notice] = await _notices(service)
    assert isinstance(notice, ApprovalRequested)
    card = notice.pull
    assert (card.repository, card.number, card.head_sha) == ("404Vector/project-enikk", 7, SHA1)
    assert [f.path for f in card.files] == ["README.md"]
    assert card.diff == "+one line"
    assert [c.name for c in card.checks] == ["checks", "protected-paths"]


async def test_failed_checks_are_reported_without_buttons(
    service: ApprovalService, pulls: FakePulls
) -> None:
    pulls.pulls[7] = _pull()
    pulls.runs[SHA1] = [
        *_passed("protected-paths"),
        CheckRun(name="checks", completed=True, conclusion="failure"),
    ]
    await service.watch_once()
    [notice] = await _notices(service)
    assert isinstance(notice, ChecksFailed)


async def test_approval_records_status_on_that_commit_and_merges(
    harness: Harness, service: ApprovalService, pulls: FakePulls
) -> None:
    pulls.pulls[7] = _pull()
    pulls.runs[SHA1] = _passed("checks")
    await service.watch_once()
    [notice] = await _notices(service)
    assert isinstance(notice, ApprovalRequested)
    harness.github.issued.clear()

    result = await service.decide(
        notice.approval_id, DecisionRequest(decision=Decision.APPROVE, decided_by=OWNER)
    )
    assert result.outcome is ResolutionOutcome.MERGED
    assert pulls.approved == [SHA1]
    assert pulls.merged == [7]
    assert harness.github.issued == [("project-enikk", TokenPurpose.APPROVE)]
    again = await service.decide(
        notice.approval_id, DecisionRequest(decision=Decision.APPROVE, decided_by=OWNER)
    )
    assert again.outcome is ResolutionOutcome.MERGED
    assert pulls.merged == [7]


async def test_commits_after_the_request_supersede_it(
    service: ApprovalService, pulls: FakePulls
) -> None:
    pulls.pulls[7] = _pull()
    pulls.runs[SHA1] = _passed("checks")
    await service.watch_once()
    [first] = await _notices(service)
    assert isinstance(first, ApprovalRequested)

    pulls.pulls[7] = _pull(sha=SHA2)
    result = await service.decide(
        first.approval_id, DecisionRequest(decision=Decision.APPROVE, decided_by=OWNER)
    )
    assert result.outcome is ResolutionOutcome.SUPERSEDED
    assert pulls.approved == []

    pulls.runs[SHA2] = _passed("checks")
    await service.watch_once()
    [second] = await _notices(service)
    assert isinstance(second, ApprovalRequested)
    assert second.pull.head_sha == SHA2


async def test_new_commit_supersedes_an_open_request(
    service: ApprovalService, pulls: FakePulls
) -> None:
    pulls.pulls[7] = _pull()
    pulls.runs[SHA1] = _passed("checks")
    await service.watch_once()
    await _notices(service)
    pulls.pulls[7] = _pull(sha=SHA2)
    pulls.runs[SHA2] = _passed("checks")
    await service.watch_once()
    notices = await _notices(service)
    # Both are created in the same instant; their order is not part of the contract.
    [resolved] = [n for n in notices if isinstance(n, ApprovalResolved)]
    [requested] = [n for n in notices if isinstance(n, ApprovalRequested)]
    assert len(notices) == 2
    assert resolved.outcome is ResolutionOutcome.SUPERSEDED
    assert requested.pull.head_sha == SHA2


async def test_rejection_closes_and_deletes_only_agent_branches(
    service: ApprovalService, pulls: FakePulls
) -> None:
    pulls.pulls[7] = _pull()
    pulls.pulls[8] = _pull(number=8, branch="feat/owner-work")
    pulls.runs[SHA1] = _passed("checks")
    await service.watch_once()
    notices = await _notices(service)
    ids = [n.approval_id for n in notices if isinstance(n, ApprovalRequested)]
    for approval_id in ids:
        result = await service.decide(
            approval_id, DecisionRequest(decision=Decision.REJECT, decided_by=OWNER)
        )
        assert result.outcome is ResolutionOutcome.REJECTED
    assert sorted(pulls.closed) == [7, 8]
    assert pulls.deleted == ["agent/abc"]
    assert pulls.merged == []


async def test_refused_merge_is_reported(service: ApprovalService, pulls: FakePulls) -> None:
    pulls.pulls[7] = _pull()
    pulls.runs[SHA1] = _passed("checks")
    pulls.merge_result = MergeResult(merged=False, detail="Head branch is out of date")
    await service.watch_once()
    [notice] = await _notices(service)
    assert isinstance(notice, ApprovalRequested)
    result = await service.decide(
        notice.approval_id, DecisionRequest(decision=Decision.APPROVE, decided_by=OWNER)
    )
    assert result.outcome is ResolutionOutcome.BLOCKED
    assert "out of date" in result.detail


async def test_requests_for_vanished_pulls_are_closed(
    harness: Harness, service: ApprovalService, pulls: FakePulls
) -> None:
    pulls.pulls[7] = _pull()
    pulls.runs[SHA1] = _passed("checks")
    await service.watch_once()
    await _notices(service)
    pulls.pulls[7] = _pull().model_copy(update={"open": False})
    await service.watch_once()
    [notice] = await _notices(service)
    assert isinstance(notice, ApprovalResolved)
    assert notice.outcome is ResolutionOutcome.CLOSED
    async with harness.db.unit_of_work() as uow:
        assert not await uow.approvals.in_state(ApprovalState.REQUESTED)


async def test_a_vanished_pull_that_was_merged_is_merged_not_closed(
    harness: Harness, service: ApprovalService, pulls: FakePulls
) -> None:
    """PR #111 (2026-11-25): merged while still requested, it was reported "closed"."""
    pulls.pulls[7] = _pull()
    pulls.runs[SHA1] = _passed("checks")
    await service.watch_once()
    await _notices(service)
    pulls.pulls[7] = _pull().model_copy(update={"open": False, "merged": True})
    await service.watch_once()
    [notice] = await _notices(service)
    assert isinstance(notice, ApprovalResolved)
    assert notice.outcome is ResolutionOutcome.MERGED
    async with harness.db.unit_of_work() as uow:
        assert not await uow.approvals.in_state(ApprovalState.REQUESTED)


async def test_drafts_and_forks_are_ignored(service: ApprovalService, pulls: FakePulls) -> None:
    pulls.pulls[7] = _pull().model_copy(update={"draft": True})
    pulls.pulls[8] = _pull(number=8).model_copy(update={"from_fork": True})
    pulls.runs[SHA1] = _passed("checks")
    await service.watch_once()
    assert await _notices(service) == []


async def test_only_the_slack_connector_reaches_the_owner(harness: Harness) -> None:
    for token in ("smoke", "claude"):
        async with harness.client(token) as client:
            with pytest.raises(CoreForbiddenError):
                await client.poll_notices(NoticePoll(wait_seconds=0))


def test_long_poll_waits_for_a_notice(service: ApprovalService) -> None:
    batch = asyncio.run(service.poll_notices(NoticePoll(wait_seconds=2)))
    assert batch.notices == ()


async def test_agent_branch_behind_main_is_updated_instead_of_asked(
    harness: Harness, service: ApprovalService, pulls: FakePulls
) -> None:
    pulls.pulls[7] = _pull().model_copy(update={"mergeable_state": "behind"})
    pulls.pulls[8] = _pull(number=8, branch="feat/owner").model_copy(
        update={"mergeable_state": "behind"}
    )
    pulls.runs[SHA1] = _passed("checks")
    await service.watch_once()
    assert pulls.updated == [7]
    notices = await _notices(service)
    assert [n.pull.number for n in notices if isinstance(n, ApprovalRequested)] == [8]
    assert ("project-enikk", TokenPurpose.APPROVE) in harness.github.issued


async def test_sensitive_paths_are_flagged_with_their_own_diff(
    service: ApprovalService, pulls: FakePulls
) -> None:
    pulls.changed = [
        FileChange(path="README.md", additions=1, deletions=0, patch="+readme"),
        FileChange(path="docs/adr/0099-x.md", additions=3, deletions=0, patch="+adr"),
        FileChange(path="NEW.md", additions=0, deletions=0, previous_path="AGENTS.md"),
    ]
    pulls.pulls[7] = _pull()
    pulls.runs[SHA1] = _passed("checks")
    await service.watch_once()
    [notice] = await _notices(service)
    assert isinstance(notice, ApprovalRequested)
    assert notice.pull.sensitive == ("docs/adr/0099-x.md", "NEW.md")
    assert "+adr" in notice.pull.sensitive_diff
    assert "+readme" not in notice.pull.sensitive_diff


async def _delegated(harness: Harness) -> str:
    """A developer session delegated from thread ``9.0``; returns its branch."""
    import uuid as _uuid

    from enikk_core_domain.model import Session
    from enikk_core_domain.testing import T0, agent, conversation, task

    session = Session.start(
        session_id=_uuid.uuid4(),
        conversation=conversation("9.0:developer"),
        agent=agent("developer"),
        now=T0,
        reply_to=conversation("9.0"),
    )
    async with harness.db.unit_of_work() as uow:
        await uow.sessions.add(session)
        await uow.tasks.add(task(session))
        await uow.commit()
    return f"agent/{session.id}"


async def _poll(service: ApprovalService) -> list[OwnerNotice]:
    return list((await service.poll_notices(NoticePoll(wait_seconds=0))).notices)


async def _ack(
    service: ApprovalService, notices: list[OwnerNotice], *cards: StatusCardPosted
) -> None:
    await service.ack_notices(
        NoticeAck(notice_ids=tuple(n.notice_id for n in notices), cards=cards)
    )


async def test_progress_of_an_agent_pr_is_one_status_card_in_its_thread(
    harness: Harness, service: ApprovalService, pulls: FakePulls
) -> None:
    from enikk_contracts.common import ConnectorKind
    from enikk_core_domain.model import ReviewMessageRef
    from enikk_core_domain.testing import conversation

    pulls.pulls[7] = _pull(branch=await _delegated(harness))
    pulls.runs[SHA1] = _passed("checks")
    await service.watch_once()

    # The card waits until the approval message is out, so it can link to it.
    [request] = await _poll(service)
    assert isinstance(request.body, ApprovalRequested)
    async with harness.db.unit_of_work() as uow:
        await uow.review_messages.put(
            ReviewMessageRef(repository=REPO, number=7, channel="C0APPROVE", ts="5.0")
        )
        await uow.commit()
    await _ack(service, [request])

    [first] = await _poll(service)
    assert isinstance(first.body, PullStatusCard)
    assert first.body.stage is PullStage.AWAITING_APPROVAL
    assert first.body.origin == conversation("9.0")
    assert first.body.card is None
    assert first.body.review is not None
    assert (first.body.review.channel, first.body.review.ts) == ("C0APPROVE", "5.0")
    card = StatusCardPosted(repository=REPO, number=7, channel="C0ASK", ts="9.5")
    await _ack(service, [first], card)

    await service.decide(
        request.body.approval_id, DecisionRequest(decision=Decision.APPROVE, decided_by=OWNER)
    )
    [merged] = await _poll(service)
    assert isinstance(merged.body, PullStatusCard)
    assert merged.body.stage is PullStage.MERGED
    assert merged.body.card is not None
    assert (merged.body.card.channel, merged.body.card.ts) == ("C0ASK", "9.5")
    async with harness.db.unit_of_work() as uow:
        assert not await uow.outbox.pending(ConnectorKind.SLACK, 10)


async def test_one_card_per_pull_request_in_a_batch(
    harness: Harness, service: ApprovalService, pulls: FakePulls
) -> None:
    pulls.pulls[7] = _pull(branch=await _delegated(harness))
    pulls.runs[SHA1] = _passed("checks")
    await service.watch_once()
    [request] = await _poll(service)
    await _ack(service, [request])
    pulls.merge_result = MergeResult(merged=False, detail="out of date")
    assert isinstance(request.body, ApprovalRequested)
    harness.clock.advance(timedelta(seconds=5))
    await service.decide(
        request.body.approval_id, DecisionRequest(decision=Decision.APPROVE, decided_by=OWNER)
    )
    # Two cards of the same PR are pending: the second waits for the first to be posted.
    [first] = await _poll(service)
    assert isinstance(first.body, PullStatusCard)
    assert first.body.stage is PullStage.AWAITING_APPROVAL
    await _ack(service, [first], StatusCardPosted(repository=REPO, number=7, channel="C", ts="1"))
    [second] = await _poll(service)
    assert isinstance(second.body, PullStatusCard)
    assert second.body.stage is PullStage.BLOCKED
    assert "out of date" in second.body.detail
    assert second.body.card is not None


async def test_failed_checks_put_their_names_on_the_card(
    harness: Harness, service: ApprovalService, pulls: FakePulls
) -> None:
    pulls.pulls[7] = _pull(branch=await _delegated(harness))
    pulls.runs[SHA1] = [
        *_passed("lint"),
        CheckRun(name="tests", completed=True, conclusion="failure"),
    ]
    await service.watch_once()
    await _ack(service, await _poll(service))
    [card] = await _poll(service)
    assert isinstance(card.body, PullStatusCard)
    assert card.body.stage is PullStage.CHECKS_FAILED
    assert "tests" in card.body.detail
    assert "lint" not in card.body.detail


async def test_owner_pulls_get_no_card(service: ApprovalService, pulls: FakePulls) -> None:
    pulls.pulls[7] = _pull(branch="feat/owner")
    pulls.runs[SHA1] = _passed("checks")
    await service.watch_once()
    bodies = await _notices(service)
    assert not any(isinstance(b, PullStatusCard) for b in bodies)


async def test_a_closed_pull_updates_its_approval_message_in_place(
    harness: Harness, service: ApprovalService, pulls: FakePulls
) -> None:
    from enikk_core_domain.model import ReviewMessageRef

    pulls.pulls[7] = _pull()
    pulls.runs[SHA1] = _passed("checks")
    await service.watch_once()
    await _notices(service)
    async with harness.db.unit_of_work() as uow:
        await uow.review_messages.put(
            ReviewMessageRef(repository=REPO, number=7, channel="C0APPROVE", ts="5.0")
        )
        await uow.commit()
    pulls.pulls[7] = _pull().model_copy(update={"open": False})
    await service.watch_once()
    [notice] = await _notices(service)
    assert isinstance(notice, ApprovalResolved)
    assert notice.replaces is not None
    assert (notice.replaces.channel, notice.replaces.ts) == ("C0APPROVE", "5.0")


async def test_a_new_commit_updates_the_owners_message_instead_of_adding_one(
    harness: Harness, service: ApprovalService, pulls: FakePulls
) -> None:
    from enikk_contracts.approvals import ApprovalOutdated
    from enikk_core_domain.model import ReviewMessageRef

    pulls.pulls[7] = _pull()
    pulls.runs[SHA1] = _passed("checks")
    await service.watch_once()
    [first] = await _notices(service)
    assert isinstance(first, ApprovalRequested)
    assert first.replaces is None
    async with harness.db.unit_of_work() as uow:
        await uow.review_messages.put(
            ReviewMessageRef(repository="404Vector/project-enikk", number=7, channel="D1", ts="5.0")
        )
        await uow.commit()

    pulls.pulls[7] = _pull(sha=SHA2)
    pulls.runs[SHA2] = [CheckRun(name="checks", completed=False, conclusion=None)]
    await service.watch_once()
    await service.watch_once()
    [outdated] = await _notices(service)
    assert isinstance(outdated, ApprovalOutdated)
    assert (outdated.replaces.channel, outdated.replaces.ts) == ("D1", "5.0")
    assert outdated.head_sha == SHA2

    pulls.runs[SHA2] = _passed("checks")
    await service.watch_once()
    [second] = await _notices(service)
    assert isinstance(second, ApprovalRequested)
    assert second.replaces is not None
    assert second.replaces.ts == "5.0"
    result = await service.decide(
        first.approval_id, DecisionRequest(decision=Decision.APPROVE, decided_by=OWNER)
    )
    assert result.outcome is ResolutionOutcome.SUPERSEDED
    assert pulls.merged == []
