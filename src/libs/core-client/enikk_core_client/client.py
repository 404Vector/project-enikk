"""Typed core API client. Services call core only through this class (ADR-0011 §4)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Final, Self

import httpx
from pydantic import ValidationError

from enikk_contracts import routes
from enikk_contracts.approvals import DecisionResult, NoticeAcked, NoticeBatch
from enikk_contracts.credentials import Credentials
from enikk_contracts.deployments import RevertResult
from enikk_contracts.diagnostics import Deployments, LogLines, Pulls, RecentTasks, TaskDetail
from enikk_contracts.digests import DigestAccepted
from enikk_contracts.errors import ErrorResponse
from enikk_contracts.events import EventAccepted
from enikk_contracts.github import GitHubHealth
from enikk_contracts.health import HealthResponse
from enikk_contracts.memory import MemoryMatches, MemoryWritten
from enikk_contracts.outbox import OutboxAcked, OutboxBatch
from enikk_contracts.prompts import (
    PromotionLinked,
    PromotionResult,
    PromptCandidateCreated,
    PromptRead,
)
from enikk_contracts.pulls import PullSubmitted
from enikk_contracts.schedules import RunList, ScheduleList, ScheduleView
from enikk_contracts.tasks import (
    CancelRequest,
    CancelResult,
    LeaseExtended,
    LeaseResponse,
    ResultAccepted,
)
from enikk_contracts.threads import ReviewThreadLinked
from enikk_contracts.transcripts import TranscriptAppended, TranscriptLoaded, TranscriptSubpaths
from enikk_core_client.errors import ERRORS_BY_CODE, CoreApiError, CoreUnavailableError
from enikk_core_client.tokens import BearerAuth

if TYPE_CHECKING:
    import uuid
    from types import TracebackType

    from enikk_contracts.approvals import DecisionRequest, NoticeAck, NoticePoll
    from enikk_contracts.base import Contract
    from enikk_contracts.credentials import CredentialRequest
    from enikk_contracts.deployments import RevertRequest
    from enikk_contracts.diagnostics import (
        DeploymentsQuery,
        LogsQuery,
        PullsQuery,
        RecentTasksQuery,
        TaskDetailQuery,
    )
    from enikk_contracts.digests import DigestRequest
    from enikk_contracts.events import InboundEvent
    from enikk_contracts.memory import (
        ForgetRequest,
        MemorySearch,
        RememberRequest,
        UpdateMemoryRequest,
    )
    from enikk_contracts.outbox import OutboxAck, OutboxPoll
    from enikk_contracts.prompts import (
        PromotionDecision,
        PromotionPosted,
        PromptCandidateRequest,
        PromptReadRequest,
    )
    from enikk_contracts.pulls import PullSubmission
    from enikk_contracts.schedules import RunsQuery, ScheduleQuery
    from enikk_contracts.tasks import Heartbeat, LeaseRequest, ResultReport
    from enikk_contracts.threads import ReviewThreadOpened
    from enikk_contracts.transcripts import SubpathQuery, TranscriptAppend, TranscriptQuery
    from enikk_core_client.tokens import TokenSource

DEFAULT_TIMEOUT: Final = 10.0
LONG_POLL_MARGIN: Final = 10.0


class CoreClient:
    def __init__(self, http: httpx.AsyncClient) -> None:
        self._http = http

    @classmethod
    def connect(
        cls,
        base_url: str,
        tokens: TokenSource,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> Self:
        http = httpx.AsyncClient(
            base_url=base_url,
            auth=BearerAuth(tokens),
            timeout=DEFAULT_TIMEOUT,
            transport=transport,
        )
        return cls(http)

    async def aclose(self) -> None:
        await self._http.aclose()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    # ------------------------------------------------------------ API

    async def health(self) -> HealthResponse:
        response = await self._send("GET", routes.HEALTH)
        return _parse(HealthResponse, response)

    async def submit_event(self, event: InboundEvent) -> EventAccepted:
        return _parse(EventAccepted, await self._post(routes.EVENTS, event))

    async def lease_task(self, request: LeaseRequest) -> LeaseResponse:
        timeout = request.wait_seconds + LONG_POLL_MARGIN
        return _parse(LeaseResponse, await self._post(routes.TASK_LEASE, request, timeout))

    async def heartbeat(self, task_id: uuid.UUID, beat: Heartbeat) -> LeaseExtended:
        path = routes.TASK_HEARTBEAT.format(task_id=task_id)
        return _parse(LeaseExtended, await self._post(path, beat))

    async def report_result(self, task_id: uuid.UUID, report: ResultReport) -> ResultAccepted:
        path = routes.TASK_RESULT.format(task_id=task_id)
        return _parse(ResultAccepted, await self._post(path, report))

    async def poll_outbox(self, poll: OutboxPoll) -> OutboxBatch:
        timeout = poll.wait_seconds + LONG_POLL_MARGIN
        return _parse(OutboxBatch, await self._post(routes.OUTBOX_POLL, poll, timeout))

    async def ack_outbox(self, ack: OutboxAck) -> OutboxAcked:
        return _parse(OutboxAcked, await self._post(routes.OUTBOX_ACK, ack))

    async def cancel_task(self, task_id: uuid.UUID) -> CancelResult:
        """Slack connector only: cancel the queued and running work of the task's thread."""
        path = routes.TASK_CANCEL.format(task_id=task_id)
        return _parse(CancelResult, await self._post(path, CancelRequest()))

    async def append_transcript(
        self, task_id: uuid.UUID, request: TranscriptAppend
    ) -> TranscriptAppended:
        path = routes.TRANSCRIPT_APPEND.format(task_id=task_id)
        return _parse(TranscriptAppended, await self._post(path, request))

    async def load_transcript(self, task_id: uuid.UUID, query: TranscriptQuery) -> TranscriptLoaded:
        path = routes.TRANSCRIPT_LOAD.format(task_id=task_id)
        return _parse(TranscriptLoaded, await self._post(path, query))

    async def transcript_subpaths(
        self, task_id: uuid.UUID, query: SubpathQuery
    ) -> TranscriptSubpaths:
        path = routes.TRANSCRIPT_SUBPATHS.format(task_id=task_id)
        return _parse(TranscriptSubpaths, await self._post(path, query))

    async def fetch_credentials(self, request: CredentialRequest) -> Credentials:
        """Credentials granted to this caller. Keep them in memory only (ADR-0013)."""
        return _parse(Credentials, await self._post(routes.CREDENTIALS, request))

    async def poll_notices(self, poll: NoticePoll) -> NoticeBatch:
        """Slack connector only: messages for the owner's DM (ADR-0020)."""
        timeout = poll.wait_seconds + LONG_POLL_MARGIN
        return _parse(NoticeBatch, await self._post(routes.NOTICES_POLL, poll, timeout))

    async def ack_notices(self, ack: NoticeAck) -> NoticeAcked:
        return _parse(NoticeAcked, await self._post(routes.NOTICES_ACK, ack))

    async def decide(self, approval_id: uuid.UUID, request: DecisionRequest) -> DecisionResult:
        """The owner's decision on a pull request. Merging can take a few seconds."""
        path = routes.APPROVAL_DECISION.format(approval_id=approval_id)
        return _parse(DecisionResult, await self._post(path, request, 60))

    async def request_revert(self, request: RevertRequest) -> RevertResult:
        """Open the revert PR of a commit (ADR-0023 §5). Slack connector only."""
        return _parse(RevertResult, await self._post(routes.REVERTS, request, 60))

    async def review_thread_opened(self, review: ReviewThreadOpened) -> ReviewThreadLinked:
        """Link a PR's review thread with the thread that asked for it. Slack connector only."""
        return _parse(ReviewThreadLinked, await self._post(routes.REVIEW_THREADS, review))

    async def propose_prompt(
        self, task_id: uuid.UUID, request: PromptCandidateRequest
    ) -> PromptCandidateCreated:
        """Create a prompt candidate and ask the owner to promote it (ADR-0032)."""
        path = routes.PROMPT_CANDIDATES.format(task_id=task_id)
        return _parse(PromptCandidateCreated, await self._post(path, request, 60))

    async def submit_pull(self, task_id: uuid.UUID, request: PullSubmission) -> PullSubmitted:
        """Core pushes the session's commits and opens or updates its PR (ADR-0037 §6b)."""
        path = routes.PULL_SUBMISSIONS.format(task_id=task_id)
        return _parse(PullSubmitted, await self._post(path, request, 180))

    async def read_prompt(self, task_id: uuid.UUID, request: PromptReadRequest) -> PromptRead:
        """One version of a role's prompt, read by core (ADR-0032 §8)."""
        path = routes.PROMPT_READ.format(task_id=task_id)
        return _parse(PromptRead, await self._post(path, request, 30))

    async def decide_promotion(
        self, promotion_id: uuid.UUID, decision: PromotionDecision
    ) -> PromotionResult:
        """The owner's promotion, rejection or revert of a prompt. Slack connector only."""
        path = routes.PROMOTION_DECISION.format(promotion_id=promotion_id)
        return _parse(PromotionResult, await self._post(path, decision, 60))

    async def promotion_posted(
        self, promotion_id: uuid.UUID, posted: PromotionPosted
    ) -> PromotionLinked:
        """The promotion request is in the owner's DM. Slack connector only."""
        path = routes.PROMOTION_POSTED.format(promotion_id=promotion_id)
        return _parse(PromotionLinked, await self._post(path, posted))

    async def request_digest(self, task_id: uuid.UUID, request: DigestRequest) -> DigestAccepted:
        path = routes.DIGESTS.format(task_id=task_id)
        return _parse(DigestAccepted, await self._post(path, request, 30))

    async def remember(self, task_id: uuid.UUID, request: RememberRequest) -> MemoryWritten:
        """Keep a note in enikk's memory, the owner's vault (ADR-0028)."""
        path = routes.MEMORY_REMEMBER.format(task_id=task_id)
        return _parse(MemoryWritten, await self._post(path, request, 60))

    async def update_memory(
        self, task_id: uuid.UUID, request: UpdateMemoryRequest
    ) -> MemoryWritten:
        path = routes.MEMORY_UPDATE.format(task_id=task_id)
        return _parse(MemoryWritten, await self._post(path, request, 60))

    async def forget_memory(self, task_id: uuid.UUID, request: ForgetRequest) -> MemoryWritten:
        path = routes.MEMORY_FORGET.format(task_id=task_id)
        return _parse(MemoryWritten, await self._post(path, request, 60))

    async def search_memory(self, task_id: uuid.UUID, request: MemorySearch) -> MemoryMatches:
        path = routes.MEMORY_SEARCH.format(task_id=task_id)
        return _parse(MemoryMatches, await self._post(path, request))

    async def diagnose_recent_tasks(
        self, task_id: uuid.UUID, query: RecentTasksQuery
    ) -> RecentTasks:
        """Read-only diagnostics (ADR-0029)."""
        path = routes.DIAG_TASKS.format(task_id=task_id)
        return _parse(RecentTasks, await self._post(path, query, 30))

    async def diagnose_task_detail(self, task_id: uuid.UUID, query: TaskDetailQuery) -> TaskDetail:
        """Read-only diagnostics (ADR-0029)."""
        path = routes.DIAG_TASK.format(task_id=task_id)
        return _parse(TaskDetail, await self._post(path, query, 30))

    async def diagnose_deployments(
        self, task_id: uuid.UUID, query: DeploymentsQuery
    ) -> Deployments:
        """Read-only diagnostics (ADR-0029)."""
        path = routes.DIAG_DEPLOYMENTS.format(task_id=task_id)
        return _parse(Deployments, await self._post(path, query, 30))

    async def diagnose_logs(self, task_id: uuid.UUID, query: LogsQuery) -> LogLines:
        """Read-only diagnostics (ADR-0029)."""
        path = routes.DIAG_LOGS.format(task_id=task_id)
        return _parse(LogLines, await self._post(path, query, 30))

    async def diagnose_pulls(self, task_id: uuid.UUID, query: PullsQuery) -> Pulls:
        """Read-only diagnostics (ADR-0029)."""
        path = routes.DIAG_PULLS.format(task_id=task_id)
        return _parse(Pulls, await self._post(path, query, 30))

    async def list_schedules(self, query: ScheduleQuery) -> ScheduleList:
        """Schedules, for the task named in the query. Scheduler only (ADR-0039)."""
        return _parse(ScheduleList, await self._post(routes.SCHEDULES_LIST, query))

    async def get_schedule(self, name: str, query: ScheduleQuery) -> ScheduleView:
        """One schedule by name. Scheduler only (ADR-0039)."""
        path = routes.SCHEDULE_GET.format(name=name)
        return _parse(ScheduleView, await self._post(path, query))

    async def list_runs(self, name: str, query: RunsQuery) -> RunList:
        """The latest runs of one schedule. Scheduler only (ADR-0039)."""
        path = routes.SCHEDULE_RUNS.format(name=name)
        return _parse(RunList, await self._post(path, query))

    async def github_health(self) -> GitHubHealth:
        """Whether core can act through the GitHub App. Smoke identity only."""
        return _parse(GitHubHealth, await self._send("GET", routes.GITHUB_HEALTH, timeout=60))

    # ------------------------------------------------------------ transport

    async def _post(
        self, path: str, body: Contract, timeout: float = DEFAULT_TIMEOUT
    ) -> httpx.Response:
        return await self._send(
            "POST",
            path,
            content=body.model_dump_json(),
            headers={"Content-Type": "application/json"},
            timeout=timeout,
        )

    async def _send(
        self,
        method: str,
        path: str,
        *,
        content: str | None = None,
        headers: dict[str, str] | None = None,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> httpx.Response:
        try:
            response = await self._http.request(
                method, path, content=content, headers=headers, timeout=timeout
            )
        except (httpx.TransportError, OSError) as exc:
            raise CoreUnavailableError(f"{method} {path}: {exc}") from exc
        if response.is_success:
            return response
        raise _error_from(response)


def _parse[T: Contract](model: type[T], response: httpx.Response) -> T:
    try:
        return model.model_validate_json(response.content)
    except ValidationError as exc:
        raise CoreApiError(
            f"unexpected {model.__name__} body from core", status=response.status_code
        ) from exc


def _error_from(response: httpx.Response) -> CoreApiError:
    try:
        error = ErrorResponse.model_validate_json(response.content)
    except ValidationError:
        if response.status_code >= 500:
            return CoreUnavailableError(
                f"core returned {response.status_code}", status=response.status_code
            )
        return CoreApiError(f"core returned {response.status_code}", status=response.status_code)
    return ERRORS_BY_CODE[error.code](error.message, status=response.status_code)
