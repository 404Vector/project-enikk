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
    ResolutionOutcome,
)
from enikk_core.approval.service import ApprovalService
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


async def test_drafts_and_forks_are_ignored(service: ApprovalService, pulls: FakePulls) -> None:
    pulls.pulls[7] = _pull().model_copy(update={"draft": True})
    pulls.pulls[8] = _pull(number=8).model_copy(update={"from_fork": True})
    pulls.runs[SHA1] = _passed("checks")
    await service.watch_once()
    assert await _notices(service) == []


async def test_only_the_slack_connector_reaches_the_owner(harness: Harness) -> None:
    for token in ("smoke", "claude", "dev"):
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
