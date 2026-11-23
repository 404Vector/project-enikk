"""Owner approval of pull requests in Slack (ADR-0020, ADR-0036).

Core → connector: notices (the approval channel, the owner's DM, the asking thread's
status card). Connector → core: the owner's decision and where status cards were posted.
"""

from __future__ import annotations

import uuid
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import Field, StringConstraints

from enikk_contracts.base import Contract
from enikk_contracts.common import ConversationRef, Identifier
from enikk_contracts.deployments import BuildFailed, DeploymentFinished
from enikk_contracts.digests import DigestFailed
from enikk_contracts.prompts import PromotionRequested
from enikk_contracts.tasks import WaitSeconds

MAX_NOTICE_BATCH = 50
ShortText = Annotated[str, StringConstraints(max_length=3_000)]
Sha = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{40}$")]


class ChangedFile(Contract):
    path: Annotated[str, StringConstraints(min_length=1, max_length=500)]
    additions: Annotated[int, Field(ge=0)]
    deletions: Annotated[int, Field(ge=0)]


class CheckResult(Contract):
    name: Annotated[str, StringConstraints(min_length=1, max_length=200)]
    conclusion: Annotated[str, StringConstraints(min_length=1, max_length=50)]


class PullRequestCard(Contract):
    """Everything the owner sees without opening GitHub."""

    repository: Annotated[str, StringConstraints(min_length=3, max_length=200)]
    """``owner/name``."""
    number: Annotated[int, Field(ge=1)]
    url: Annotated[str, StringConstraints(min_length=1, max_length=500)]
    title: Annotated[str, StringConstraints(min_length=1, max_length=300)]
    author: Annotated[str, StringConstraints(min_length=1, max_length=100)]
    branch: Annotated[str, StringConstraints(min_length=1, max_length=255)]
    head_sha: Sha
    body: ShortText = ""
    files: Annotated[tuple[ChangedFile, ...], Field(max_length=100)] = ()
    more_files: Annotated[int, Field(ge=0)] = 0
    checks: Annotated[tuple[CheckResult, ...], Field(max_length=50)] = ()
    diff: Annotated[str, StringConstraints(max_length=500_000)] = ""
    diff_truncated: bool = False
    sensitive: Annotated[tuple[str, ...], Field(max_length=100)] = ()
    """Changed sensitive paths (ADR-0024): the owner must confirm before approving."""
    sensitive_diff: Annotated[str, StringConstraints(max_length=40_000)] = ""
    """The diff of just those files, shown before everything else."""


class ReviewMessage(Contract):
    """A Slack message updated in place: the approval message of a pull request
    (ADR-0020 §4, ADR-0036) or its status card in the asking thread."""

    channel: Identifier
    ts: Identifier


class ApprovalRequested(Contract):
    kind: Literal["approval-requested"] = "approval-requested"
    approval_id: uuid.UUID
    pull: PullRequestCard
    replaces: ReviewMessage | None = None
    """The PR's earlier message: update it instead of posting a new one."""


class ChecksFailed(Contract):
    kind: Literal["checks-failed"] = "checks-failed"
    pull: PullRequestCard
    replaces: ReviewMessage | None = None


class ApprovalOutdated(Contract):
    """New commits arrived while the owner's message still asks about an older one.

    The message drops its buttons and says the new commit is being checked; it is
    updated again with the new request when the checks finish.
    """

    kind: Literal["approval-outdated"] = "approval-outdated"
    repository: str
    number: Annotated[int, Field(ge=1)]
    url: str
    title: str
    head_sha: Sha
    replaces: ReviewMessage


class ResolutionOutcome(StrEnum):
    MERGED = "merged"
    REJECTED = "rejected"
    SUPERSEDED = "superseded"
    """New commits arrived after the request; a fresh request follows when checks pass."""
    BLOCKED = "blocked"
    """Approved, but GitHub refused the merge; ``detail`` says why."""
    CLOSED = "closed"
    """The PR was closed or merged outside Slack."""


class ApprovalResolved(Contract):
    kind: Literal["approval-resolved"] = "approval-resolved"
    approval_id: uuid.UUID
    repository: str
    number: Annotated[int, Field(ge=1)]
    url: str
    title: str
    outcome: ResolutionOutcome
    detail: ShortText = ""
    replaces: ReviewMessage | None = None
    """The PR's approval message: update it instead of posting a new one."""


class PullStage(StrEnum):
    """Where an agent's pull request stands, as its status card shows (ADR-0036)."""

    CHECKING = "checking"
    """New commits are being checked."""
    CHECKS_FAILED = "checks-failed"
    AWAITING_APPROVAL = "awaiting-approval"
    MERGED = "merged"
    """Approved and merged; the image build and deployment follow if there are any."""
    BLOCKED = "blocked"
    """Approved, but GitHub refused the merge."""
    REJECTED = "rejected"
    CLOSED = "closed"
    DEPLOYED = "deployed"
    DEPLOY_FAILED = "deploy-failed"
    BUILD_FAILED = "build-failed"
    ROLLED_BACK = "rolled-back"
    """The merge commit was reverted and the revert deployed."""
    ROLLBACK_FAILED = "rollback-failed"


class PullStatusCard(Contract):
    """The one message in the asking thread that shows an agent PR's status (ADR-0036).

    Core fills ``card`` and ``review`` when it hands the notice out: ``card`` is the
    card already posted (update it), ``review`` the PR's approval message (link to it).
    """

    kind: Literal["pull-status"] = "pull-status"
    repository: Annotated[str, StringConstraints(min_length=3, max_length=200)]
    number: Annotated[int, Field(ge=1)]
    url: Annotated[str, StringConstraints(min_length=1, max_length=500)]
    title: Annotated[str, StringConstraints(max_length=300)]
    origin: ConversationRef
    stage: PullStage
    commit: Sha | None = None
    detail: ShortText = ""
    card: ReviewMessage | None = None
    review: ReviewMessage | None = None


OwnerNoticeBody = Annotated[
    ApprovalRequested
    | ChecksFailed
    | ApprovalOutdated
    | ApprovalResolved
    | DeploymentFinished
    | BuildFailed
    | PromotionRequested
    | DigestFailed
    | PullStatusCard,
    Field(discriminator="kind"),
]


class OwnerNotice(Contract):
    notice_id: uuid.UUID
    body: OwnerNoticeBody


class NoticePoll(Contract):
    wait_seconds: WaitSeconds = 20
    limit: Annotated[int, Field(ge=1, le=MAX_NOTICE_BATCH)] = 10


class NoticeBatch(Contract):
    notices: tuple[OwnerNotice, ...]


class StatusCardPosted(Contract):
    """A pull request's status card was posted at ``channel``/``ts`` (ADR-0036)."""

    repository: Annotated[str, StringConstraints(min_length=3, max_length=200)]
    number: Annotated[int, Field(ge=1)]
    channel: Identifier
    ts: Identifier


class NoticeAck(Contract):
    notice_ids: Annotated[tuple[uuid.UUID, ...], Field(min_length=1, max_length=MAX_NOTICE_BATCH)]
    cards: Annotated[tuple[StatusCardPosted, ...], Field(max_length=MAX_NOTICE_BATCH)] = ()
    """Status cards posted while delivering these notices; later ones update them."""


class NoticeAcked(Contract):
    acknowledged: Annotated[int, Field(ge=0)]


class Decision(StrEnum):
    APPROVE = "approve"
    REJECT = "reject"


class DecisionRequest(Contract):
    """The owner pressed a button. The connector has checked who pressed it (I-01)."""

    decision: Decision
    decided_by: Identifier


class DecisionResult(Contract):
    outcome: ResolutionOutcome
    detail: ShortText = ""
