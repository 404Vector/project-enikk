"""Typed core API client. Services call core only through this class (ADR-0011 §4)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Final, Self

import httpx
from pydantic import ValidationError

from enikk_contracts import routes
from enikk_contracts.approvals import DecisionResult, NoticeAcked, NoticeBatch
from enikk_contracts.credentials import Credentials
from enikk_contracts.errors import ErrorResponse
from enikk_contracts.events import EventAccepted
from enikk_contracts.github import GitHubHealth, GitHubToken
from enikk_contracts.health import HealthResponse
from enikk_contracts.outbox import OutboxAcked, OutboxBatch
from enikk_contracts.tasks import (
    ClaimRequest,
    DelegationAccepted,
    LeaseExtended,
    LeaseResponse,
    ResultAccepted,
)
from enikk_contracts.transcripts import TranscriptAppended, TranscriptLoaded, TranscriptSubpaths
from enikk_core_client.errors import ERRORS_BY_CODE, CoreApiError, CoreUnavailableError
from enikk_core_client.tokens import BearerAuth

if TYPE_CHECKING:
    import uuid
    from types import TracebackType

    from enikk_contracts.approvals import DecisionRequest, NoticeAck, NoticePoll
    from enikk_contracts.base import Contract
    from enikk_contracts.credentials import CredentialRequest
    from enikk_contracts.events import InboundEvent
    from enikk_contracts.github import GitHubTokenRequest
    from enikk_contracts.outbox import OutboxAck, OutboxPoll
    from enikk_contracts.tasks import DelegateRequest, Heartbeat, LeaseRequest, ResultReport
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

    async def claim_task(self, task_id: uuid.UUID) -> LeaseResponse:
        """Isolated workers only: lease the task this Job was started for (ADR-0021)."""
        path = routes.TASK_CLAIM.format(task_id=task_id)
        return _parse(LeaseResponse, await self._post(path, ClaimRequest()))

    async def delegate_task(
        self, task_id: uuid.UUID, request: DelegateRequest
    ) -> DelegationAccepted:
        """Queue ``request`` for a delegate of the running task's agent (ADR-0022)."""
        path = routes.TASK_DELEGATE.format(task_id=task_id)
        return _parse(DelegationAccepted, await self._post(path, request))

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

    async def github_token(self, request: GitHubTokenRequest) -> GitHubToken:
        """Isolated workers only: a development token for one repository (I-32)."""
        return _parse(GitHubToken, await self._post(routes.GITHUB_TOKEN, request))

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
