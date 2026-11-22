"""Watches pull requests, asks the owner, and acts on the owner's decision (ADR-0020).

- When every check of a PR's head commit has finished, the owner gets a DM: a request
  with buttons if the checks passed, a notice if they failed. Once per commit.
- Approving records ``enikk/owner-approval`` on exactly that commit and squash-merges
  it, only if the PR still points at that commit (I-33). Rejecting closes the PR.
- Only this module records approvals; it is called for the owner's Slack actions alone.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import TYPE_CHECKING, Final, Protocol

from enikk_contracts.approvals import (
    ApprovalRequested,
    ApprovalResolved,
    ChangedFile,
    CheckResult,
    ChecksFailed,
    Decision,
    DecisionResult,
    NoticeAcked,
    NoticeBatch,
    OwnerNotice,
    PullRequestCard,
    ResolutionOutcome,
)
from enikk_core_domain.errors import DependencyUnavailableError, NotFoundError
from enikk_core_domain.model import Approval, ApprovalState, Notice
from enikk_github.permissions import TokenPurpose

if TYPE_CHECKING:
    import uuid
    from collections.abc import Awaitable, Callable

    from enikk_contracts.approvals import (
        DecisionRequest,
        NoticeAck,
        NoticePoll,
        OwnerNoticeBody,
    )
    from enikk_core.github import GitHubAppApi
    from enikk_core_domain.ports import UnitOfWork, UnitOfWorkFactory
    from enikk_core_domain.repositories import RepositoryPolicy
    from enikk_github.app import InstallationToken
    from enikk_github.pulls import CheckRun, FileChange, MergeResult, PullInfo
    from enikk_kernel.clock import Clock
    from enikk_kernel.ids import IdGenerator

    Sleep = Callable[[float], Awaitable[None]]

AGENT_BRANCH_PREFIX: Final = "agent/"
MAX_FILES: Final = 100
POLL_INTERVAL: Final = timedelta(seconds=1)

_log = logging.getLogger(__name__)

_OUTCOME_OF_STATE: Final = {
    ApprovalState.MERGED: ResolutionOutcome.MERGED,
    ApprovalState.REJECTED: ResolutionOutcome.REJECTED,
    ApprovalState.SUPERSEDED: ResolutionOutcome.SUPERSEDED,
    ApprovalState.BLOCKED: ResolutionOutcome.BLOCKED,
    ApprovalState.CLOSED: ResolutionOutcome.CLOSED,
}


class PullsApi(Protocol):
    async def open_pulls(self, token: InstallationToken, base: str = "main") -> list[PullInfo]: ...
    async def get(self, token: InstallationToken, number: int) -> PullInfo: ...
    async def check_runs(self, token: InstallationToken, sha: str) -> list[CheckRun]: ...
    async def files(self, token: InstallationToken, number: int) -> list[FileChange]: ...
    async def diff(self, token: InstallationToken, number: int) -> tuple[str, bool]: ...
    async def approve(self, token: InstallationToken, sha: str) -> None: ...
    async def merge(self, token: InstallationToken, pull: PullInfo) -> MergeResult: ...
    async def close(self, token: InstallationToken, number: int) -> None: ...
    async def delete_branch(self, token: InstallationToken, branch: str) -> None: ...


class ApprovalService:
    def __init__(
        self,
        *,
        uow: UnitOfWorkFactory,
        app: GitHubAppApi,
        pulls: PullsApi,
        policy: RepositoryPolicy,
        clock: Clock,
        ids: IdGenerator,
        sleep: Sleep,
    ) -> None:
        self._uow = uow
        self._app = app
        self._pulls = pulls
        self._policy = policy
        self._clock = clock
        self._ids = ids
        self._sleep = sleep

    # ------------------------------------------------------------ watching

    async def watch_once(self) -> None:
        for repository in self._policy.repositories:
            try:
                token = await self._app.installation_token(repository.name, TokenPurpose.WATCH)
            except DependencyUnavailableError as exc:
                _log.warning("cannot watch %s: %s", repository.name, exc)
                continue
            try:
                await self._watch(token)
            finally:
                await self._app.revoke(token)

    async def _watch(self, token: InstallationToken) -> None:
        full_name = f"{self._app.owner}/{token.repository}"
        pulls = await self._pulls.open_pulls(token)
        await self._close_vanished(full_name, {p.number for p in pulls})
        for pull in pulls:
            if pull.draft or pull.from_fork:
                continue
            async with self._uow() as uow:
                if await uow.approvals.find(full_name, pull.number, pull.head_sha):
                    continue
            runs = await self._pulls.check_runs(token, pull.head_sha)
            if not runs or not all(r.completed for r in runs):
                continue
            card = await self._card(token, full_name, pull, runs)
            await self._record(full_name, pull, card, failed=any(r.failed for r in runs))

    async def _close_vanished(self, full_name: str, open_numbers: set[int]) -> None:
        async with self._uow() as uow:
            for approval in await uow.approvals.in_state(ApprovalState.REQUESTED):
                if approval.repository == full_name and approval.number not in open_numbers:
                    await self._resolve(uow, approval, ApprovalState.CLOSED, "")
            await uow.commit()

    async def _record(
        self, full_name: str, pull: PullInfo, card: PullRequestCard, *, failed: bool
    ) -> None:
        now = self._clock.now()
        async with self._uow() as uow:
            if await uow.approvals.find(full_name, pull.number, pull.head_sha):
                return
            for older in await uow.approvals.in_state(ApprovalState.REQUESTED):
                if older.repository == full_name and older.number == pull.number:
                    await self._resolve(uow, older, ApprovalState.SUPERSEDED, "new commits")
            approval = Approval(
                id=self._ids.new(),
                repository=full_name,
                number=pull.number,
                head_sha=pull.head_sha,
                branch=pull.branch,
                title=pull.title[:300],
                url=pull.url,
                state=ApprovalState.CHECKS_FAILED if failed else ApprovalState.REQUESTED,
                created_at=now,
            )
            await uow.approvals.add(approval)
            body: OwnerNoticeBody = (
                ChecksFailed(pull=card)
                if failed
                else ApprovalRequested(approval_id=approval.id, pull=card)
            )
            await uow.notices.add(Notice(id=self._ids.new(), body=body, created_at=now))
            await uow.commit()
        _log.info(
            "%s#%d %s: %s",
            full_name,
            pull.number,
            pull.head_sha[:7],
            "checks failed" if failed else "approval requested",
        )

    async def _card(
        self, token: InstallationToken, full_name: str, pull: PullInfo, runs: list[CheckRun]
    ) -> PullRequestCard:
        files = await self._pulls.files(token, pull.number)
        diff, truncated = await self._pulls.diff(token, pull.number)
        return PullRequestCard(
            repository=full_name,
            number=pull.number,
            url=pull.url,
            title=pull.title[:300],
            author=pull.author[:100],
            branch=pull.branch,
            head_sha=pull.head_sha,
            body=pull.body[:3000],
            files=tuple(
                ChangedFile(path=f.path[:500], additions=f.additions, deletions=f.deletions)
                for f in files[:MAX_FILES]
            ),
            more_files=max(0, len(files) - MAX_FILES),
            checks=tuple(
                CheckResult(name=r.name[:200], conclusion=(r.conclusion or "unknown")[:50])
                for r in runs[:50]
            ),
            diff=diff,
            diff_truncated=truncated,
        )

    # ------------------------------------------------------------ deciding

    async def decide(self, approval_id: uuid.UUID, request: DecisionRequest) -> DecisionResult:
        async with self._uow() as uow:
            approval = await uow.approvals.get_for_update(approval_id)
            if approval is None:
                raise NotFoundError(f"approval {approval_id} not found")
            if approval.state is not ApprovalState.REQUESTED:
                outcome = _OUTCOME_OF_STATE.get(approval.state, ResolutionOutcome.CLOSED)
                return DecisionResult(outcome=outcome, detail="already decided")
            name = approval.repository.split("/", 1)[1]
            token = await self._app.installation_token(name, TokenPurpose.APPROVE)
            try:
                state, detail = await self._act(token, approval, request.decision)
            finally:
                await self._app.revoke(token)
            resolved = approval.resolve(state, self._clock.now(), by=request.decided_by)
            await uow.approvals.update(resolved)
            await uow.commit()
        _log.info(
            "%s#%d: %s by the owner -> %s",
            approval.repository,
            approval.number,
            request.decision,
            state,
        )
        return DecisionResult(outcome=_OUTCOME_OF_STATE[state], detail=detail[:3000])

    async def _act(
        self, token: InstallationToken, approval: Approval, decision: Decision
    ) -> tuple[ApprovalState, str]:
        pull = await self._pulls.get(token, approval.number)
        if not pull.open:
            return ApprovalState.CLOSED, "the PR is no longer open"
        if pull.head_sha != approval.head_sha:
            return (
                ApprovalState.SUPERSEDED,
                "new commits were pushed; a new request follows when the checks pass",
            )
        if decision is Decision.REJECT:
            await self._pulls.close(token, pull.number)
            if pull.branch.startswith(AGENT_BRANCH_PREFIX):
                await self._pulls.delete_branch(token, pull.branch)
            return ApprovalState.REJECTED, ""
        await self._pulls.approve(token, approval.head_sha)
        merged = await self._pulls.merge(token, pull)
        if merged.merged:
            return ApprovalState.MERGED, ""
        return ApprovalState.BLOCKED, merged.detail

    async def _resolve(
        self, uow: UnitOfWork, approval: Approval, state: ApprovalState, detail: str
    ) -> None:
        now = self._clock.now()
        await uow.approvals.update(approval.resolve(state, now))
        await uow.notices.add(
            Notice(
                id=self._ids.new(),
                body=ApprovalResolved(
                    approval_id=approval.id,
                    repository=approval.repository,
                    number=approval.number,
                    url=approval.url,
                    title=approval.title,
                    outcome=_OUTCOME_OF_STATE[state],
                    detail=detail,
                ),
                created_at=now,
            )
        )

    # ------------------------------------------------------------ the owner's DM queue

    async def poll_notices(self, poll: NoticePoll) -> NoticeBatch:
        deadline = self._clock.now() + timedelta(seconds=poll.wait_seconds)
        while True:
            async with self._uow() as uow:
                pending = await uow.notices.pending(poll.limit)
            if pending or self._clock.now() >= deadline:
                return NoticeBatch(
                    notices=tuple(OwnerNotice(notice_id=n.id, body=n.body) for n in pending)
                )
            await self._sleep(POLL_INTERVAL.total_seconds())

    async def ack_notices(self, ack: NoticeAck) -> NoticeAcked:
        async with self._uow() as uow:
            changed = 0
            for notice in await uow.notices.get_many(ack.notice_ids):
                delivered = notice.mark_delivered()
                if delivered is not notice:
                    await uow.notices.update(delivered)
                    changed += 1
            await uow.commit()
        return NoticeAcked(acknowledged=changed)
