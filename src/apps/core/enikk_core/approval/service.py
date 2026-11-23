"""Watches pull requests, asks the owner, and acts on the owner's decision (ADR-0020).

- When every check of a PR's head commit has finished, the approval channel (or the
  owner's DM, ADR-0036) gets a request with buttons if the checks passed, a notice if
  they failed. Once per commit. An agent PR's asking thread gets one status card.
- Approving records ``enikk/owner-approval`` on exactly that commit and squash-merges
  it, only if the PR still points at that commit (I-33). Rejecting closes the PR.
- Only this module records approvals; it is called for the owner's Slack actions alone.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import TYPE_CHECKING, Final, Protocol

from enikk_contracts.approvals import (
    ApprovalOutdated,
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
    PullStage,
    PullStatusCard,
    ResolutionOutcome,
    ReviewMessage,
)
from enikk_core_domain.errors import DependencyUnavailableError, NotFoundError
from enikk_core_domain.model import Approval, ApprovalState, Notice, ReviewMessageRef
from enikk_github.permissions import TokenPurpose

if TYPE_CHECKING:
    import uuid
    from collections.abc import Awaitable, Callable, Sequence

    from enikk_contracts.approvals import (
        DecisionRequest,
        NoticeAck,
        NoticePoll,
        OwnerNoticeBody,
    )
    from enikk_core.github import GitHubAppApi
    from enikk_core.threads import ThreadNotes
    from enikk_core_domain.ports import UnitOfWork, UnitOfWorkFactory
    from enikk_core_domain.repositories import RepositoryPolicy, SensitivePaths
    from enikk_github.app import InstallationToken
    from enikk_github.pulls import CheckRun, FileChange, MergeResult, PullInfo
    from enikk_kernel.clock import Clock
    from enikk_kernel.ids import IdGenerator

    Sleep = Callable[[float], Awaitable[None]]

AGENT_BRANCH_PREFIX: Final = "agent/"
MAX_FILES: Final = 100
SENSITIVE_DIFF_LIMIT: Final = 40_000
POLL_INTERVAL: Final = timedelta(seconds=1)

_log = logging.getLogger(__name__)

PASSING: Final = frozenset({"success", "neutral", "skipped"})

_STAGE_OF_DECISION: Final = {
    ApprovalState.MERGED: PullStage.MERGED,
    ApprovalState.REJECTED: PullStage.REJECTED,
    ApprovalState.BLOCKED: PullStage.BLOCKED,
}

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
    async def update_branch(self, token: InstallationToken, pull: PullInfo) -> bool: ...


class ApprovalService:
    def __init__(
        self,
        *,
        uow: UnitOfWorkFactory,
        app: GitHubAppApi,
        pulls: PullsApi,
        policy: RepositoryPolicy,
        sensitive: SensitivePaths,
        clock: Clock,
        ids: IdGenerator,
        sleep: Sleep,
        threads: ThreadNotes | None = None,
    ) -> None:
        self._uow = uow
        self._app = app
        self._pulls = pulls
        self._policy = policy
        self._sensitive = sensitive
        self._clock = clock
        self._ids = ids
        self._sleep = sleep
        self._threads = threads

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
                await self._outdate(full_name, pull)
                continue
            if await self._brought_up_to_date(token, pull):
                continue
            card = await self._pull_card(token, full_name, pull, runs)
            await self._record(full_name, pull, card, failed=any(r.failed for r in runs))

    async def _brought_up_to_date(self, token: InstallationToken, pull: PullInfo) -> bool:
        """An agent branch behind main is updated instead of asked about.

        ``main`` requires branches to be up to date, so approving it would only be
        refused. The update is a new commit; its checks run and then the owner is asked.
        Other branches belong to the owner, whom the ruleset lets update them.
        """
        if not pull.branch.startswith(AGENT_BRANCH_PREFIX):
            return False
        current = await self._pulls.get(token, pull.number)
        if current.mergeable_state != "behind" or current.head_sha != pull.head_sha:
            return False
        writer = await self._app.installation_token(token.repository, TokenPurpose.APPROVE)
        try:
            updated = await self._pulls.update_branch(writer, current)
        finally:
            await self._app.revoke(writer)
        _log.info(
            "%s#%d is behind main: %s",
            token.repository,
            pull.number,
            "updated the branch" if updated else "could not update the branch",
        )
        return updated

    async def _outdate(self, full_name: str, pull: PullInfo) -> None:
        """A new commit is being checked: retire the request for the older one in place.

        The owner's message loses its buttons and says the new commit is being checked,
        instead of a separate "superseded" message; it is updated again with the new
        request when the checks finish (ADR-0020 §4).
        """
        async with self._uow() as uow:
            ref = await uow.review_messages.get(full_name, pull.number)
            older = [
                a
                for a in await uow.approvals.in_state(ApprovalState.REQUESTED)
                if a.repository == full_name
                and a.number == pull.number
                and a.head_sha != pull.head_sha
            ]
            if not older or ref is None:
                return
            for approval in older:
                await self._resolve(uow, approval, ApprovalState.SUPERSEDED, "", notify=False)
            await self._card(
                uow,
                full_name,
                pull.number,
                pull.url,
                pull.title,
                pull.branch,
                PullStage.CHECKING,
                commit=pull.head_sha,
            )
            now = self._clock.now()
            await uow.notices.add(
                Notice(
                    id=self._ids.new(),
                    body=ApprovalOutdated(
                        repository=full_name,
                        number=pull.number,
                        url=pull.url,
                        title=pull.title[:300],
                        head_sha=pull.head_sha,
                        replaces=ReviewMessage(channel=ref.channel, ts=ref.ts),
                    ),
                    created_at=now,
                )
            )
            await uow.commit()
        _log.info("%s#%d %s: older request retired", full_name, pull.number, pull.head_sha[:7])

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
            ref = await uow.review_messages.get(full_name, pull.number)
            replaces = None if ref is None else ReviewMessage(channel=ref.channel, ts=ref.ts)
            for older in await uow.approvals.in_state(ApprovalState.REQUESTED):
                if older.repository == full_name and older.number == pull.number:
                    await self._resolve(
                        uow, older, ApprovalState.SUPERSEDED, "new commits", notify=ref is None
                    )
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
                ChecksFailed(pull=card, replaces=replaces)
                if failed
                else ApprovalRequested(approval_id=approval.id, pull=card, replaces=replaces)
            )
            await uow.notices.add(Notice(id=self._ids.new(), body=body, created_at=now))
            names = ", ".join(c.name for c in card.checks if c.conclusion not in PASSING)
            await self._card(
                uow,
                full_name,
                pull.number,
                pull.url,
                pull.title,
                pull.branch,
                PullStage.CHECKS_FAILED if failed else PullStage.AWAITING_APPROVAL,
                commit=pull.head_sha,
                detail=f"실패한 검사: {names or '알 수 없음'}" if failed else "",
            )
            await uow.commit()
        _log.info(
            "%s#%d %s: %s",
            full_name,
            pull.number,
            pull.head_sha[:7],
            "checks failed" if failed else "approval requested",
        )

    async def _pull_card(
        self, token: InstallationToken, full_name: str, pull: PullInfo, runs: list[CheckRun]
    ) -> PullRequestCard:
        files = await self._pulls.files(token, pull.number)
        diff, truncated = await self._pulls.diff(token, pull.number)
        flagged = [
            f
            for f in files
            if self._sensitive.matches(f.path)
            or (f.previous_path is not None and self._sensitive.matches(f.previous_path))
        ]
        sensitive_diff = "\n".join(
            f"--- {f.path}\n{f.patch or '(diff 없음: 바이너리이거나 너무 큼)'}" for f in flagged
        )
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
            sensitive=tuple(f.path[:500] for f in flagged[:100]),
            sensitive_diff=sensitive_diff[:SENSITIVE_DIFF_LIMIT],
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
            stage = _STAGE_OF_DECISION.get(state)
            if stage is not None:
                await self._card(
                    uow,
                    approval.repository,
                    approval.number,
                    approval.url,
                    approval.title,
                    approval.branch,
                    stage,
                    commit=approval.head_sha,
                    detail=detail,
                )
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

    async def _card(
        self,
        uow: UnitOfWork,
        repository: str,
        number: int,
        url: str,
        title: str,
        branch: str,
        stage: PullStage,
        *,
        commit: str | None = None,
        detail: str = "",
    ) -> None:
        if self._threads is not None:
            await self._threads.card(
                uow,
                branch,
                repository=repository,
                number=number,
                url=url,
                title=title,
                stage=stage,
                commit=commit,
                detail=detail,
            )

    async def _resolve(
        self,
        uow: UnitOfWork,
        approval: Approval,
        state: ApprovalState,
        detail: str,
        *,
        notify: bool = True,
    ) -> None:
        now = self._clock.now()
        if state is ApprovalState.CLOSED:
            await self._card(
                uow,
                approval.repository,
                approval.number,
                approval.url,
                approval.title,
                approval.branch,
                PullStage.CLOSED,
            )
        await uow.approvals.update(approval.resolve(state, now))
        if not notify:
            return
        ref = await uow.review_messages.get(approval.repository, approval.number)
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
                    replaces=None if ref is None else ReviewMessage(channel=ref.channel, ts=ref.ts),
                ),
                created_at=now,
            )
        )

    # ------------------------------------------------------------ the notice queue

    async def poll_notices(self, poll: NoticePoll) -> NoticeBatch:
        deadline = self._clock.now() + timedelta(seconds=poll.wait_seconds)
        while True:
            async with self._uow() as uow:
                pending = await uow.notices.pending(poll.limit)
                handed = await _hand_out(uow, pending)
            if handed or self._clock.now() >= deadline:
                return NoticeBatch(notices=tuple(handed))
            await self._sleep(POLL_INTERVAL.total_seconds())

    async def ack_notices(self, ack: NoticeAck) -> NoticeAcked:
        async with self._uow() as uow:
            for card in ack.cards:
                await uow.status_cards.put(
                    ReviewMessageRef(
                        repository=card.repository,
                        number=card.number,
                        channel=card.channel,
                        ts=card.ts,
                    )
                )
            changed = 0
            for notice in await uow.notices.get_many(ack.notice_ids):
                delivered = notice.mark_delivered()
                if delivered is not notice:
                    await uow.notices.update(delivered)
                    changed += 1
            await uow.commit()
        return NoticeAcked(acknowledged=changed)


def _pull_of(body: OwnerNoticeBody) -> tuple[str, int] | None:
    """The pull request a notice is about, if any."""
    if isinstance(body, ApprovalRequested | ChecksFailed):
        return body.pull.repository, body.pull.number
    if isinstance(body, ApprovalOutdated | ApprovalResolved | PullStatusCard):
        return body.repository, body.number
    return None


async def _hand_out(uow: UnitOfWork, pending: Sequence[Notice]) -> list[OwnerNotice]:
    """The notices to hand out now, status cards with where they already are (ADR-0036).

    A status card waits while another notice of the same PR is pending in this batch:
    the approval message must exist before the card links to it, and a card posted in
    this batch is known only once the connector acknowledges it.
    """
    others: set[tuple[str, int]] = set()
    for notice in pending:
        key = _pull_of(notice.body)
        if key is not None and not isinstance(notice.body, PullStatusCard):
            others.add(key)
    carded: set[tuple[str, int]] = set()
    handed: list[OwnerNotice] = []
    for notice in pending:
        body = notice.body
        if isinstance(body, PullStatusCard):
            key = (body.repository, body.number)
            if key in others or key in carded:
                continue
            carded.add(key)
            card = await uow.status_cards.get(*key)
            review = await uow.review_messages.get(*key)
            body = body.model_copy(
                update={
                    "card": None
                    if card is None
                    else ReviewMessage(channel=card.channel, ts=card.ts),
                    "review": None
                    if review is None
                    else ReviewMessage(channel=review.channel, ts=review.ts),
                }
            )
        handed.append(OwnerNotice(notice_id=notice.id, body=body))
    return handed
