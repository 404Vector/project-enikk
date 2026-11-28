"""HTTP API: authenticates the caller, parses contracts, calls a use case."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid  # noqa: TC003 - FastAPI reads endpoint annotations at runtime
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Final

from fastapi import FastAPI, Request, Response
from fastapi.routing import APIRoute
from pydantic import ValidationError

from enikk_contracts import routes
from enikk_contracts.approvals import (
    DecisionRequest,
    DecisionResult,
    NoticeAck,
    NoticeAcked,
    NoticeBatch,
    NoticePoll,
)
from enikk_contracts.attachments import (
    AttachmentContent,
    AttachmentFetch,
    AttachmentStored,
    AttachmentUpload,
)
from enikk_contracts.common import ConnectorKind
from enikk_contracts.credentials import CredentialRequest, Credentials
from enikk_contracts.delegation import (
    AskCreated,
    AskDecision,
    AskRequest,
    AskResult,
    Delegated,
    DelegateRequest,
)
from enikk_contracts.deployments import RevertRequest, RevertResult
from enikk_contracts.diagnostics import (
    Deployments,
    DeploymentsQuery,
    LogLines,
    LogsQuery,
    Pulls,
    PullsQuery,
    RecentTasks,
    RecentTasksQuery,
    TaskDetail,
    TaskDetailQuery,
    VaultActions,
    VaultActionsQuery,
)
from enikk_contracts.errors import ErrorCode, ErrorResponse
from enikk_contracts.events import EventAccepted, InboundEvent
from enikk_contracts.github import GitHubHealth
from enikk_contracts.health import HealthResponse
from enikk_contracts.mcp import McpJwks
from enikk_contracts.memory import (
    ForgetRequest,
    MemoryMatches,
    MemorySearch,
    MemoryWritten,
    NoteChangeCreated,
    NoteChangeDecision,
    NoteChangeLinked,
    NoteChangePosted,
    NoteChangeRequest,
    NoteChangeResult,
    NoteContent,
    NoteRead,
    NoteWrite,
    NoteWritten,
    RememberRequest,
    UpdateMemoryRequest,
)
from enikk_contracts.outbox import OutboxAck, OutboxAcked, OutboxBatch, OutboxFile, OutboxPoll
from enikk_contracts.prompts import (
    PromotionDecision,
    PromotionLinked,
    PromotionPosted,
    PromotionResult,
    PromptCandidateCreated,
    PromptCandidateRequest,
    PromptRead,
    PromptReadRequest,
)
from enikk_contracts.schedules import (
    RunList,
    RunNow,
    RunsQuery,
    RunView,
    ScheduleCreate,
    ScheduleDeleted,
    ScheduleList,
    SchedulePause,
    ScheduleQuery,
    ScheduleTicked,
    ScheduleUpdate,
    ScheduleView,
)
from enikk_contracts.tasks import (
    CancelRequest,
    CancelResult,
    Heartbeat,
    LeaseExtended,
    LeaseRequest,
    LeaseResponse,
    ResultAccepted,
    ResultReport,
)
from enikk_contracts.thread_actions import (
    CancelScheduledRequest,
    EmojiList,
    EmojiQuery,
    MessageLink,
    MessageLinkRequest,
    ScheduledCancelled,
    ScheduledMessage,
    ScheduledMessages,
    ScheduledQuery,
    ScheduleMessageRequest,
    SlackWorkspaceReceived,
    SlackWorkspaceReport,
    ThreadActionDone,
    ThreadFileRequest,
    ThreadReactionRequest,
)
from enikk_contracts.threads import ReviewThreadLinked, ReviewThreadOpened
from enikk_contracts.transcripts import (
    SubpathQuery,
    TranscriptAppend,
    TranscriptAppended,
    TranscriptLoaded,
    TranscriptQuery,
    TranscriptSubpaths,
)
from enikk_core.security import declared_roles, requires
from enikk_core_domain.auth import ConnectorRole, RuntimeRole, SchedulerRole
from enikk_core_domain.errors import (
    AuthenticationError,
    ConflictError,
    DependencyUnavailableError,
    InvalidRequestError,
    NotFoundError,
    PermissionDeniedError,
    UnknownAgentError,
)
from enikk_core_domain.model import WorkerScope
from enikk_kernel.errors import ConfigurationError

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable

    from enikk_contracts.base import Contract
    from enikk_core.approval.service import ApprovalService
    from enikk_core.asks import AskService
    from enikk_core.credentials import CredentialIssuer
    from enikk_core.deployments import DeploymentService
    from enikk_core.diagnostics import DiagnosticsService
    from enikk_core.github import GitHubHealthCheck
    from enikk_core.mcp_tokens import McpTokenIssuer
    from enikk_core.memory import MemoryService
    from enikk_core.prompts import PromptService
    from enikk_core.schedules import ScheduleService
    from enikk_core.security import Gatekeeper
    from enikk_core.threads import ReviewLinks
    from enikk_core.usecases import CoreUseCases
    from enikk_core_domain.auth import Principal

    ErrorHandler = Callable[[Request, Exception], Awaitable[Response]]

SERVICE_NAME: Final = "enikk-core"
_log = logging.getLogger(__name__)

_ERRORS: Final[tuple[tuple[type[Exception], int, ErrorCode], ...]] = (
    (AuthenticationError, 401, ErrorCode.UNAUTHENTICATED),
    (PermissionDeniedError, 403, ErrorCode.FORBIDDEN),
    (NotFoundError, 404, ErrorCode.NOT_FOUND),
    (ConflictError, 409, ErrorCode.CONFLICT),
    (UnknownAgentError, 422, ErrorCode.INVALID_REQUEST),
    (InvalidRequestError, 422, ErrorCode.INVALID_REQUEST),
    (ValidationError, 422, ErrorCode.INVALID_REQUEST),
    (DependencyUnavailableError, 503, ErrorCode.UNAVAILABLE),
)


class CoreServices:
    """Everything the HTTP layer needs, assembled by the composition root."""

    def __init__(
        self,
        *,
        version: str,
        usecases: CoreUseCases,
        credentials: CredentialIssuer,
        gatekeeper: Gatekeeper,
        github: GitHubHealthCheck,
        reaper_interval_seconds: float,
        approvals: ApprovalService | None = None,
        watch_interval_seconds: float = 30.0,
        deployments: DeploymentService | None = None,
        deployment_interval_seconds: float = 30.0,
        review_links: ReviewLinks | None = None,
        memory: MemoryService | None = None,
        memory_interval_seconds: float = 60.0,
        diagnostics: DiagnosticsService | None = None,
        prompts: PromptService | None = None,
        schedules: ScheduleService | None = None,
        mcp_tokens: McpTokenIssuer | None = None,
        asks: AskService | None = None,
    ) -> None:
        self.version = version
        self.usecases = usecases
        self.credentials = credentials
        self.gatekeeper = gatekeeper
        self.github = github
        self.reaper_interval_seconds = reaper_interval_seconds
        self.approvals = approvals
        self.watch_interval_seconds = watch_interval_seconds
        self.deployments = deployments
        self.deployment_interval_seconds = deployment_interval_seconds
        self.review_links = review_links
        self.memory = memory
        self.memory_interval_seconds = memory_interval_seconds
        self.diagnostics = diagnostics
        self.prompts = prompts
        self.schedules = schedules
        self.mcp_tokens = mcp_tokens
        self.asks = asks


def create_app(services: CoreServices, *, run_reaper: bool = True) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        loops: list[asyncio.Task[None]] = []
        if run_reaper:
            loops.append(asyncio.create_task(_reap_forever(services)))
            if services.approvals is not None:
                loops.append(asyncio.create_task(_watch_forever(services)))
            if services.deployments is not None:
                loops.append(asyncio.create_task(_watch_deployments_forever(services)))
            if services.memory is not None:
                loops.append(asyncio.create_task(_sync_memory_forever(services)))
        try:
            yield
        finally:
            for loop in loops:
                loop.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await loop

    app = FastAPI(title=SERVICE_NAME, version=services.version, lifespan=lifespan)
    gate = services.gatekeeper
    usecases = services.usecases

    for error_type, status, code in _ERRORS:
        app.add_exception_handler(error_type, _error_handler(status, code))
    app.add_exception_handler(Exception, _internal_error)

    @app.get(routes.HEALTH, response_model=HealthResponse)
    async def healthz() -> HealthResponse:
        return HealthResponse(service=SERVICE_NAME, version=services.version)

    @app.get(routes.MCP_JWKS, responses={200: {"model": McpJwks}})
    async def mcp_jwks() -> Response:
        """Public keys only; the gateway reads them without a token (ADR-0039 §2, I-28).

        503 without a key: the gateway reads the keys once when it starts and does not
        start without them, so it restarts until core has its source key."""
        jwks = McpJwks() if services.mcp_tokens is None else services.mcp_tokens.jwks()
        if not jwks.keys:
            return Response(
                '{"error":"no MCP signing key"}', status_code=503, media_type="application/json"
            )
        return _json(jwks)

    @app.post(routes.EVENTS, responses={200: {"model": EventAccepted}})
    @requires("connector")
    async def submit_event(request: Request) -> Response:
        role = _connector(await gate.admit(request))
        event = await _parse(request, InboundEvent)
        if event.conversation.connector is not role.connector:
            raise PermissionDeniedError(f"connector {role.connector} cannot post to another")
        return _json(await usecases.accept_event(event))

    @app.post(routes.OUTBOX_POLL, responses={200: {"model": OutboxBatch}})
    @requires("connector")
    async def poll_outbox(request: Request) -> Response:
        role = _connector(await gate.admit(request))
        poll = await _parse(request, OutboxPoll)
        return _json(await usecases.poll_outbox(role.connector, poll))

    @app.post(routes.OUTBOX_ACK, responses={200: {"model": OutboxAcked}})
    @requires("connector")
    async def ack_outbox(request: Request) -> Response:
        role = _connector(await gate.admit(request))
        ack = await _parse(request, OutboxAck)
        return _json(await usecases.ack_outbox(role.connector, ack))

    @app.post(routes.SLACK_WORKSPACE, responses={200: {"model": SlackWorkspaceReceived}})
    @requires("connector")
    async def slack_workspace(request: Request) -> Response:
        role = _connector(await gate.admit(request))
        if role.connector is not ConnectorKind.SLACK:
            raise PermissionDeniedError("only the Slack connector reports the workspace")
        body = await _parse(request, SlackWorkspaceReport)
        return _json(usecases.report_workspace(body))

    @app.post(routes.OUTBOX_FILE, responses={200: {"model": OutboxFile}})
    @requires("connector")
    async def outbox_file(message_id: uuid.UUID, request: Request) -> Response:
        role = _connector(await gate.admit(request))
        return _json(await usecases.outbox_file(role.connector, message_id))

    @app.post(routes.TASK_CANCEL, responses={200: {"model": CancelResult}})
    @requires("connector")
    async def cancel_task(task_id: uuid.UUID, request: Request) -> Response:
        role = _owner_channel(await gate.admit(request))
        await _parse(request, CancelRequest)
        return _json(await usecases.cancel_task(role.connector, task_id))

    @app.post(routes.TASK_ATTACHMENT_UPLOAD, responses={200: {"model": AttachmentStored}})
    @requires("connector")
    async def upload_attachment(task_id: uuid.UUID, index: int, request: Request) -> Response:
        role = _connector(await gate.admit(request))
        upload = await _parse(request, AttachmentUpload)
        return _json(await usecases.upload_attachment(role.connector, task_id, index, upload))

    @app.post(routes.TASK_ATTACHMENT_FETCH, responses={200: {"model": AttachmentContent}})
    @requires("runtime")
    async def fetch_attachment(task_id: uuid.UUID, index: int, request: Request) -> Response:
        scope = _worker(await gate.admit(request))
        fetch = await _parse(request, AttachmentFetch)
        return _json(await usecases.fetch_attachment(scope, task_id, index, fetch))

    @app.post(routes.TASK_LEASE, responses={200: {"model": LeaseResponse}})
    @requires("runtime")
    async def lease_task(request: Request) -> Response:
        role = _runtime(await gate.admit(request))
        lease = await _parse(request, LeaseRequest)
        return _json(await usecases.lease_task(role.engine, lease))

    @app.post(routes.DIAG_TASKS, responses={200: {"model": RecentTasks}})
    @requires("runtime")
    async def diagnose_recent_tasks(task_id: uuid.UUID, request: Request) -> Response:
        scope = _worker(await gate.admit(request))
        body = await _parse(request, RecentTasksQuery)
        await usecases.verify_lease(scope, task_id, body.lease_id)
        return _json(await _diagnostics(services).recent_tasks(body))

    @app.post(routes.DIAG_TASK, responses={200: {"model": TaskDetail}})
    @requires("runtime")
    async def diagnose_task_detail(task_id: uuid.UUID, request: Request) -> Response:
        scope = _worker(await gate.admit(request))
        body = await _parse(request, TaskDetailQuery)
        await usecases.verify_lease(scope, task_id, body.lease_id)
        return _json(await _diagnostics(services).task_detail(body))

    @app.post(routes.DIAG_DEPLOYMENTS, responses={200: {"model": Deployments}})
    @requires("runtime")
    async def diagnose_deployments(task_id: uuid.UUID, request: Request) -> Response:
        scope = _worker(await gate.admit(request))
        body = await _parse(request, DeploymentsQuery)
        await usecases.verify_lease(scope, task_id, body.lease_id)
        return _json(await _diagnostics(services).deployments(body))

    @app.post(routes.DIAG_LOGS, responses={200: {"model": LogLines}})
    @requires("runtime")
    async def diagnose_logs(task_id: uuid.UUID, request: Request) -> Response:
        scope = _worker(await gate.admit(request))
        body = await _parse(request, LogsQuery)
        await usecases.verify_lease(scope, task_id, body.lease_id)
        return _json(await _diagnostics(services).logs(body))

    @app.post(routes.DIAG_PULLS, responses={200: {"model": Pulls}})
    @requires("runtime")
    async def diagnose_pulls(task_id: uuid.UUID, request: Request) -> Response:
        scope = _worker(await gate.admit(request))
        body = await _parse(request, PullsQuery)
        await usecases.verify_lease(scope, task_id, body.lease_id)
        return _json(await _diagnostics(services).pulls(body))

    @app.post(routes.DIAG_VAULT_ACTIONS, responses={200: {"model": VaultActions}})
    @requires("runtime")
    async def diagnose_vault_actions(task_id: uuid.UUID, request: Request) -> Response:
        scope = _worker(await gate.admit(request))
        body = await _parse(request, VaultActionsQuery)
        await usecases.verify_lease(scope, task_id, body.lease_id)
        return _json(await _diagnostics(services).vault_actions(body))

    @app.post(routes.MEMORY_REMEMBER, responses={200: {"model": MemoryWritten}})
    @requires("runtime")
    async def remember(task_id: uuid.UUID, request: Request) -> Response:
        scope = _worker(await gate.admit(request))
        body = await _parse(request, RememberRequest)
        await usecases.verify_lease(scope, task_id, body.lease_id)
        return _json(await _memory(services).remember(task_id, body))

    @app.post(routes.MEMORY_UPDATE, responses={200: {"model": MemoryWritten}})
    @requires("runtime")
    async def update_memory(task_id: uuid.UUID, request: Request) -> Response:
        scope = _worker(await gate.admit(request))
        body = await _parse(request, UpdateMemoryRequest)
        await usecases.verify_lease(scope, task_id, body.lease_id)
        return _json(await _memory(services).update(task_id, body))

    @app.post(routes.MEMORY_FORGET, responses={200: {"model": MemoryWritten}})
    @requires("runtime")
    async def forget_memory(task_id: uuid.UUID, request: Request) -> Response:
        scope = _worker(await gate.admit(request))
        body = await _parse(request, ForgetRequest)
        await usecases.verify_lease(scope, task_id, body.lease_id)
        return _json(await _memory(services).forget(task_id, body))

    @app.post(routes.MEMORY_SEARCH, responses={200: {"model": MemoryMatches}})
    @requires("runtime")
    async def search_memory(task_id: uuid.UUID, request: Request) -> Response:
        scope = _worker(await gate.admit(request))
        body = await _parse(request, MemorySearch)
        await usecases.verify_lease(scope, task_id, body.lease_id)
        return _json(await _memory(services).search(body))

    @app.post(routes.NOTE_READ, responses={200: {"model": NoteContent}})
    @requires("runtime")
    async def read_note(task_id: uuid.UUID, request: Request) -> Response:
        scope = _worker(await gate.admit(request))
        body = await _parse(request, NoteRead)
        await usecases.verify_lease(scope, task_id, body.lease_id)
        return _json(await _memory(services).read_note(body))

    @app.post(routes.NOTE_WRITE, responses={200: {"model": NoteWritten}})
    @requires("runtime")
    async def write_note(task_id: uuid.UUID, request: Request) -> Response:
        scope = _worker(await gate.admit(request))
        body = await _parse(request, NoteWrite)
        await usecases.verify_lease(scope, task_id, body.lease_id)
        return _json(await _memory(services).write_note(task_id, body))

    @app.post(routes.THREAD_FILE, responses={200: {"model": ThreadActionDone}})
    @requires("runtime")
    async def thread_file(task_id: uuid.UUID, request: Request) -> Response:
        scope = _worker(await gate.admit(request))
        body = await _parse(request, ThreadFileRequest)
        await usecases.post_file(scope, task_id, body)
        return _json(ThreadActionDone(action="file"))

    @app.post(routes.THREAD_REACTION, responses={200: {"model": ThreadActionDone}})
    @requires("runtime")
    async def thread_reaction(task_id: uuid.UUID, request: Request) -> Response:
        scope = _worker(await gate.admit(request))
        body = await _parse(request, ThreadReactionRequest)
        note = await usecases.react(scope, task_id, body)
        return _json(ThreadActionDone(action="reaction", note=note))

    @app.post(routes.THREAD_LINK, responses={200: {"model": MessageLink}})
    @requires("runtime")
    async def thread_link(task_id: uuid.UUID, request: Request) -> Response:
        scope = _worker(await gate.admit(request))
        body = await _parse(request, MessageLinkRequest)
        return _json(await usecases.message_link(scope, task_id, body))

    @app.post(routes.THREAD_EMOJI, responses={200: {"model": EmojiList}})
    @requires("runtime")
    async def thread_emoji(task_id: uuid.UUID, request: Request) -> Response:
        scope = _worker(await gate.admit(request))
        body = await _parse(request, EmojiQuery)
        return _json(await usecases.list_emoji(scope, task_id, body))

    @app.post(routes.THREAD_SCHEDULE, responses={200: {"model": ScheduledMessage}})
    @requires("runtime")
    async def thread_schedule(task_id: uuid.UUID, request: Request) -> Response:
        scope = _worker(await gate.admit(request))
        body = await _parse(request, ScheduleMessageRequest)
        return _json(await usecases.schedule_message(scope, task_id, body))

    @app.post(routes.THREAD_SCHEDULED_LIST, responses={200: {"model": ScheduledMessages}})
    @requires("runtime")
    async def thread_scheduled(task_id: uuid.UUID, request: Request) -> Response:
        scope = _worker(await gate.admit(request))
        body = await _parse(request, ScheduledQuery)
        return _json(await usecases.scheduled_messages(scope, task_id, body))

    @app.post(routes.TASK_DELEGATE, responses={200: {"model": Delegated}})
    @requires("runtime")
    async def task_delegate(task_id: uuid.UUID, request: Request) -> Response:
        scope = _worker(await gate.admit(request))
        body = await _parse(request, DelegateRequest)
        return _json(await usecases.delegate(scope, task_id, body))

    @app.post(routes.TASK_ASK, responses={200: {"model": AskCreated}})
    @requires("runtime")
    async def task_ask(task_id: uuid.UUID, request: Request) -> Response:
        scope = _worker(await gate.admit(request))
        body = await _parse(request, AskRequest)
        await usecases.verify_lease(scope, task_id, body.lease_id)
        return _json(await _asks(services).ask(task_id, body))

    @app.post(routes.ASK_DECISION, responses={200: {"model": AskResult}})
    @requires("connector")
    async def decide_ask(request_id: uuid.UUID, request: Request) -> Response:
        _owner_channel(await gate.admit(request))
        body = await _parse(request, AskDecision)
        return _json(await _asks(services).decide(request_id, body))

    @app.post(routes.THREAD_SCHEDULED_CANCEL, responses={200: {"model": ScheduledCancelled}})
    @requires("runtime")
    async def thread_scheduled_cancel(task_id: uuid.UUID, request: Request) -> Response:
        scope = _worker(await gate.admit(request))
        body = await _parse(request, CancelScheduledRequest)
        return _json(await usecases.cancel_scheduled(scope, task_id, body))

    @app.post(routes.PROMPT_CANDIDATES, responses={200: {"model": PromptCandidateCreated}})
    @requires("runtime")
    async def propose_prompt(task_id: uuid.UUID, request: Request) -> Response:
        role = _runtime(await gate.admit(request))
        body = await _parse(request, PromptCandidateRequest)
        await usecases.verify_lease(WorkerScope.shared(role.engine), task_id, body.lease_id)
        return _json(await _prompts(services).propose(task_id, body))

    @app.post(routes.PROMPT_READ, responses={200: {"model": PromptRead}})
    @requires("runtime")
    async def read_prompt(task_id: uuid.UUID, request: Request) -> Response:
        role = _runtime(await gate.admit(request))
        body = await _parse(request, PromptReadRequest)
        await usecases.verify_lease(WorkerScope.shared(role.engine), task_id, body.lease_id)
        return _json(await _prompts(services).read(task_id, body))

    @app.post(routes.NOTE_CHANGE, responses={200: {"model": NoteChangeCreated}})
    @requires("runtime")
    async def request_note_change(task_id: uuid.UUID, request: Request) -> Response:
        scope = _worker(await gate.admit(request))
        body = await _parse(request, NoteChangeRequest)
        await usecases.verify_lease(scope, task_id, body.lease_id)
        return _json(await _memory(services).request_change(task_id, body))

    @app.post(routes.NOTE_CHANGE_DECISION, responses={200: {"model": NoteChangeResult}})
    @requires("connector")
    async def decide_note_change(change_id: uuid.UUID, request: Request) -> Response:
        _owner_channel(await gate.admit(request))
        body = await _parse(request, NoteChangeDecision)
        return _json(await _memory(services).decide_change(change_id, body))

    @app.post(routes.NOTE_CHANGE_POSTED, responses={200: {"model": NoteChangeLinked}})
    @requires("connector")
    async def note_change_posted(change_id: uuid.UUID, request: Request) -> Response:
        _owner_channel(await gate.admit(request))
        body = await _parse(request, NoteChangePosted)
        return _json(await _memory(services).change_posted(change_id, body))

    @app.post(routes.PROMOTION_DECISION, responses={200: {"model": PromotionResult}})
    @requires("connector")
    async def decide_promotion(promotion_id: uuid.UUID, request: Request) -> Response:
        _owner_channel(await gate.admit(request))
        body = await _parse(request, PromotionDecision)
        return _json(await _prompts(services).decide(promotion_id, body))

    @app.post(routes.PROMOTION_POSTED, responses={200: {"model": PromotionLinked}})
    @requires("connector")
    async def promotion_posted(promotion_id: uuid.UUID, request: Request) -> Response:
        _owner_channel(await gate.admit(request))
        body = await _parse(request, PromotionPosted)
        return _json(await _prompts(services).posted(promotion_id, body))

    @app.post(routes.TASK_HEARTBEAT, responses={200: {"model": LeaseExtended}})
    @requires("runtime")
    async def heartbeat(task_id: uuid.UUID, request: Request) -> Response:
        scope = _worker(await gate.admit(request))
        beat = await _parse(request, Heartbeat)
        return _json(await usecases.heartbeat(scope, task_id, beat))

    @app.post(routes.TASK_RESULT, responses={200: {"model": ResultAccepted}})
    @requires("runtime")
    async def report_result(task_id: uuid.UUID, request: Request) -> Response:
        scope = _worker(await gate.admit(request))
        report = await _parse(request, ResultReport)
        return _json(await usecases.report_result(scope, task_id, report))

    @app.post(routes.TRANSCRIPT_APPEND, responses={200: {"model": TranscriptAppended}})
    @requires("runtime")
    async def append_transcript(task_id: uuid.UUID, request: Request) -> Response:
        scope = _worker(await gate.admit(request))
        body = await _parse(request, TranscriptAppend)
        return _json(await usecases.append_transcript(scope, task_id, body))

    @app.post(routes.TRANSCRIPT_LOAD, responses={200: {"model": TranscriptLoaded}})
    @requires("runtime")
    async def load_transcript(task_id: uuid.UUID, request: Request) -> Response:
        scope = _worker(await gate.admit(request))
        query = await _parse(request, TranscriptQuery)
        return _json(await usecases.load_transcript(scope, task_id, query))

    @app.post(routes.TRANSCRIPT_SUBPATHS, responses={200: {"model": TranscriptSubpaths}})
    @requires("runtime")
    async def transcript_subpaths(task_id: uuid.UUID, request: Request) -> Response:
        scope = _worker(await gate.admit(request))
        query = await _parse(request, SubpathQuery)
        return _json(await usecases.transcript_subpaths(scope, task_id, query))

    @app.post(routes.CREDENTIALS, responses={200: {"model": Credentials}})
    @requires("connector", "runtime", "tool")
    async def fetch_credentials(request: Request) -> Response:
        principal = await gate.admit(request)
        wanted = await _parse(request, CredentialRequest)
        response = _json(services.credentials.issue(principal, wanted))
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.get(routes.GITHUB_HEALTH, responses={200: {"model": GitHubHealth}})
    @requires("connector")
    async def github_health(request: Request) -> Response:
        role = _connector(await gate.admit(request))
        if role.connector is not ConnectorKind.SMOKE:
            raise PermissionDeniedError("only the smoke check reads the GitHub status")
        return _json(await services.github.check())

    @app.post(routes.NOTICES_POLL, responses={200: {"model": NoticeBatch}})
    @requires("connector")
    async def poll_notices(request: Request) -> Response:
        _owner_channel(await gate.admit(request))
        poll = await _parse(request, NoticePoll)
        return _json(await _approvals(services).poll_notices(poll))

    @app.post(routes.NOTICES_ACK, responses={200: {"model": NoticeAcked}})
    @requires("connector")
    async def ack_notices(request: Request) -> Response:
        _owner_channel(await gate.admit(request))
        ack = await _parse(request, NoticeAck)
        return _json(await _approvals(services).ack_notices(ack))

    @app.post(routes.REVERTS, responses={200: {"model": RevertResult}})
    @requires("connector")
    async def request_revert(request: Request) -> Response:
        _owner_channel(await gate.admit(request))
        body = await _parse(request, RevertRequest)
        return _json(await _deployments(services).request_revert(body))

    @app.post(routes.REVIEW_THREADS, responses={200: {"model": ReviewThreadLinked}})
    @requires("connector")
    async def review_thread_opened(request: Request) -> Response:
        _owner_channel(await gate.admit(request))
        body = await _parse(request, ReviewThreadOpened)
        return _json(await _review_links(services).opened(body))

    @app.post(routes.APPROVAL_DECISION, responses={200: {"model": DecisionResult}})
    @requires("connector")
    async def decide(approval_id: uuid.UUID, request: Request) -> Response:
        _owner_channel(await gate.admit(request))
        body = await _parse(request, DecisionRequest)
        return _json(await _approvals(services).decide(approval_id, body))

    @app.post(routes.SCHEDULES_LIST, responses={200: {"model": ScheduleList}})
    @requires("scheduler")
    async def list_schedules(request: Request) -> Response:
        _scheduler(await gate.admit(request))
        body = await _parse(request, ScheduleQuery)
        return _json(await _schedules(services).list(body))

    @app.post(routes.SCHEDULES, responses={200: {"model": ScheduleView}})
    @requires("scheduler")
    async def create_schedule(request: Request) -> Response:
        _scheduler(await gate.admit(request))
        body = await _parse(request, ScheduleCreate)
        return _json(await _schedules(services).create(body))

    @app.post(routes.SCHEDULE_GET, responses={200: {"model": ScheduleView}})
    @requires("scheduler")
    async def get_schedule(name: str, request: Request) -> Response:
        _scheduler(await gate.admit(request))
        body = await _parse(request, ScheduleQuery)
        return _json(await _schedules(services).get(name, body))

    @app.post(routes.SCHEDULE_UPDATE, responses={200: {"model": ScheduleView}})
    @requires("scheduler")
    async def update_schedule(name: str, request: Request) -> Response:
        _scheduler(await gate.admit(request))
        body = await _parse(request, ScheduleUpdate)
        return _json(await _schedules(services).update(name, body))

    @app.post(routes.SCHEDULE_PAUSE, responses={200: {"model": ScheduleView}})
    @requires("scheduler")
    async def pause_schedule(name: str, request: Request) -> Response:
        _scheduler(await gate.admit(request))
        body = await _parse(request, SchedulePause)
        return _json(await _schedules(services).pause(name, body))

    @app.post(routes.SCHEDULE_DELETE, responses={200: {"model": ScheduleDeleted}})
    @requires("scheduler")
    async def delete_schedule(name: str, request: Request) -> Response:
        _scheduler(await gate.admit(request))
        body = await _parse(request, ScheduleQuery)
        return _json(await _schedules(services).delete(name, body))

    @app.post(routes.SCHEDULES_TICK, responses={200: {"model": ScheduleTicked}})
    @requires("scheduler")
    async def tick_schedules(request: Request) -> Response:
        _scheduler(await gate.admit(request))
        return _json(await _schedules(services).tick())

    @app.post(routes.SCHEDULE_RUN_NOW, responses={200: {"model": RunView}})
    @requires("scheduler")
    async def run_schedule_now(name: str, request: Request) -> Response:
        _scheduler(await gate.admit(request))
        body = await _parse(request, RunNow)
        return _json(await _schedules(services).run_now(name, body))

    @app.post(routes.SCHEDULE_RUNS, responses={200: {"model": RunList}})
    @requires("scheduler")
    async def schedule_runs(name: str, request: Request) -> Response:
        _scheduler(await gate.admit(request))
        body = await _parse(request, RunsQuery)
        return _json(await _schedules(services).runs(name, body))

    verify_declared_roles(app)
    return app


UNAUTHENTICATED: Final = frozenset({routes.HEALTH, routes.MCP_JWKS})
"""Endpoints without a token (I-28): health, and the public keys of the MCP tokens."""


def verify_declared_roles(app: FastAPI) -> None:
    """Fail app creation when an endpoint outside ``UNAUTHENTICATED`` declares no role (I-28)."""
    undeclared = [
        route.path
        for route in app.routes
        if isinstance(route, APIRoute)
        and route.path not in UNAUTHENTICATED
        and not declared_roles(route.endpoint)
    ]
    if undeclared:
        raise ConfigurationError(f"endpoints without a declared role: {undeclared}")


def _json(model: Contract, status: int = 200) -> Response:
    return Response(model.model_dump_json(), status_code=status, media_type="application/json")


async def _parse[T: Contract](request: Request, model: type[T]) -> T:
    return model.model_validate_json(await request.body())


def _connector(principal: Principal) -> ConnectorRole:
    if not isinstance(principal.role, ConnectorRole):
        raise PermissionDeniedError("connector role required")
    return principal.role


def _worker(principal: Principal) -> WorkerScope:
    """Shared runtimes see their engine's tasks."""
    if isinstance(principal.role, RuntimeRole):
        return WorkerScope.shared(principal.role.engine)
    raise PermissionDeniedError("worker role required")


def _runtime(principal: Principal) -> RuntimeRole:
    if not isinstance(principal.role, RuntimeRole):
        raise PermissionDeniedError("runtime role required")
    return principal.role


def _scheduler(principal: Principal) -> SchedulerRole:
    if not isinstance(principal.role, SchedulerRole):
        raise PermissionDeniedError("scheduler role required")
    return principal.role


def _owner_channel(principal: Principal) -> ConnectorRole:
    """The owner's DM (approvals) is reached through the Slack connector only (ADR-0020)."""
    role = _connector(principal)
    if role.connector is not ConnectorKind.SLACK:
        raise PermissionDeniedError("only the Slack connector talks to the owner")
    return role


def _diagnostics(services: CoreServices) -> DiagnosticsService:
    if services.diagnostics is None:
        raise DependencyUnavailableError("diagnostics are not enabled")
    return services.diagnostics


def _memory(services: CoreServices) -> MemoryService:
    if services.memory is None:
        raise DependencyUnavailableError("enikk's memory is not enabled")
    return services.memory


def _asks(services: CoreServices) -> AskService:
    if services.asks is None:
        raise DependencyUnavailableError("requests between agents are not enabled")
    return services.asks


def _prompts(services: CoreServices) -> PromptService:
    if services.prompts is None:
        raise DependencyUnavailableError("prompt candidates are not enabled in core")
    return services.prompts


def _schedules(services: CoreServices) -> ScheduleService:
    if services.schedules is None:
        raise DependencyUnavailableError("schedules are not enabled")
    return services.schedules


def _deployments(services: CoreServices) -> DeploymentService:
    if services.deployments is None:
        raise DependencyUnavailableError("deployment notices are not enabled")
    return services.deployments


def _review_links(services: CoreServices) -> ReviewLinks:
    if services.review_links is None:
        raise DependencyUnavailableError("review thread links are not enabled")
    return services.review_links


def _approvals(services: CoreServices) -> ApprovalService:
    if services.approvals is None:
        raise DependencyUnavailableError("Slack approval is not enabled")
    return services.approvals


async def _watch_forever(services: CoreServices) -> None:
    approvals = services.approvals
    if approvals is None:
        return
    while True:
        try:
            await approvals.watch_once()
        except Exception:
            _log.exception("pull request watch iteration failed")
        await asyncio.sleep(services.watch_interval_seconds)


async def _watch_deployments_forever(services: CoreServices) -> None:
    deployments = services.deployments
    if deployments is None:
        return
    while True:
        try:
            await deployments.watch_once()
        except Exception:
            _log.exception("deployment watch iteration failed")
        await asyncio.sleep(services.deployment_interval_seconds)


async def _sync_memory_forever(services: CoreServices) -> None:
    memory = services.memory
    if memory is None:
        return
    while True:
        try:
            await memory.sync_once()
        except Exception:
            _log.exception("vault sync iteration failed")
        await asyncio.sleep(services.memory_interval_seconds)


async def _reap_forever(services: CoreServices) -> None:
    while True:
        try:
            await services.usecases.reap_expired_leases()
        except Exception:
            _log.exception("lease reaper iteration failed")
        try:
            if services.asks is not None:
                await services.asks.expire_due()
        except Exception:
            _log.exception("expiring requests between agents failed")
        await asyncio.sleep(services.reaper_interval_seconds)


def _error_handler(status: int, code: ErrorCode) -> ErrorHandler:
    async def handle(_: Request, exc: Exception) -> Response:
        if status >= 500:
            _log.warning("%s: %s", code, exc)
        return _json(ErrorResponse(code=code, message=str(exc)[:2000]), status)

    return handle


async def _internal_error(_: Request, exc: Exception) -> Response:
    _log.error("unhandled error", exc_info=exc)
    return _json(ErrorResponse(code=ErrorCode.INTERNAL, message="internal error"), 500)
