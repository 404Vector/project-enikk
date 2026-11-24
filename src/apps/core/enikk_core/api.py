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
from enikk_contracts.common import ConnectorKind
from enikk_contracts.credentials import CredentialRequest, Credentials
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
)
from enikk_contracts.digests import DigestAccepted, DigestRequest
from enikk_contracts.errors import ErrorCode, ErrorResponse
from enikk_contracts.events import EventAccepted, InboundEvent
from enikk_contracts.github import GitHubHealth, GitHubToken, GitHubTokenRequest
from enikk_contracts.health import HealthResponse
from enikk_contracts.memory import (
    ForgetRequest,
    MemoryMatches,
    MemorySearch,
    MemoryWritten,
    RememberRequest,
    UpdateMemoryRequest,
)
from enikk_contracts.outbox import OutboxAck, OutboxAcked, OutboxBatch, OutboxPoll
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
from enikk_contracts.pulls import PullSubmission, PullSubmitted
from enikk_contracts.tasks import (
    CancelRequest,
    CancelResult,
    ClaimRequest,
    DelegateRequest,
    DelegationAccepted,
    Heartbeat,
    LeaseExtended,
    LeaseRequest,
    LeaseResponse,
    ResultAccepted,
    ResultReport,
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
from enikk_core_domain.auth import ConnectorRole, DevAgentRole, RuntimeRole
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
    from enikk_core.credentials import CredentialIssuer
    from enikk_core.deployments import DeploymentService
    from enikk_core.diagnostics import DiagnosticsService
    from enikk_core.digests import DigestService
    from enikk_core.dispatcher import DevJobDispatcher
    from enikk_core.github import DevTokenIssuer, GitHubHealthCheck
    from enikk_core.memory import MemoryService
    from enikk_core.prompts import PromptService
    from enikk_core.security import Gatekeeper
    from enikk_core.submissions import PullSubmissions
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
        dev_tokens: DevTokenIssuer,
        reaper_interval_seconds: float,
        dispatcher: DevJobDispatcher | None = None,
        dispatch_interval_seconds: float = 10.0,
        approvals: ApprovalService | None = None,
        watch_interval_seconds: float = 30.0,
        deployments: DeploymentService | None = None,
        deployment_interval_seconds: float = 30.0,
        review_links: ReviewLinks | None = None,
        memory: MemoryService | None = None,
        memory_interval_seconds: float = 60.0,
        diagnostics: DiagnosticsService | None = None,
        prompts: PromptService | None = None,
        digests: DigestService | None = None,
        digest_interval_seconds: float = 300.0,
        submissions: PullSubmissions | None = None,
    ) -> None:
        self.version = version
        self.usecases = usecases
        self.credentials = credentials
        self.gatekeeper = gatekeeper
        self.github = github
        self.dev_tokens = dev_tokens
        self.reaper_interval_seconds = reaper_interval_seconds
        self.dispatcher = dispatcher
        self.dispatch_interval_seconds = dispatch_interval_seconds
        self.approvals = approvals
        self.watch_interval_seconds = watch_interval_seconds
        self.deployments = deployments
        self.deployment_interval_seconds = deployment_interval_seconds
        self.review_links = review_links
        self.memory = memory
        self.memory_interval_seconds = memory_interval_seconds
        self.diagnostics = diagnostics
        self.prompts = prompts
        self.digests = digests
        self.digest_interval_seconds = digest_interval_seconds
        self.submissions = submissions


def create_app(services: CoreServices, *, run_reaper: bool = True) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        loops: list[asyncio.Task[None]] = []
        if run_reaper:
            loops.append(asyncio.create_task(_reap_forever(services)))
            if services.dispatcher is not None:
                loops.append(asyncio.create_task(_dispatch_forever(services)))
            if services.approvals is not None:
                loops.append(asyncio.create_task(_watch_forever(services)))
            if services.deployments is not None:
                loops.append(asyncio.create_task(_watch_deployments_forever(services)))
            if services.memory is not None:
                loops.append(asyncio.create_task(_sync_memory_forever(services)))
            if services.digests is not None:
                loops.append(asyncio.create_task(_digest_forever(services)))
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

    @app.post(routes.TASK_CANCEL, responses={200: {"model": CancelResult}})
    @requires("connector")
    async def cancel_task(task_id: uuid.UUID, request: Request) -> Response:
        role = _owner_channel(await gate.admit(request))
        await _parse(request, CancelRequest)
        return _json(await usecases.cancel_task(role.connector, task_id))

    @app.post(routes.TASK_LEASE, responses={200: {"model": LeaseResponse}})
    @requires("runtime")
    async def lease_task(request: Request) -> Response:
        role = _runtime(await gate.admit(request))
        lease = await _parse(request, LeaseRequest)
        return _json(await usecases.lease_task(role.engine, lease))

    @app.post(routes.TASK_CLAIM, responses={200: {"model": LeaseResponse}})
    @requires("dev-agent")
    async def claim_task(task_id: uuid.UUID, request: Request) -> Response:
        _dev_agent(await gate.admit(request))
        await _parse(request, ClaimRequest)
        return _json(await usecases.claim_task(task_id))

    @app.post(routes.DIAG_TASKS, responses={200: {"model": RecentTasks}})
    @requires("runtime", "dev-agent")
    async def diagnose_recent_tasks(task_id: uuid.UUID, request: Request) -> Response:
        scope = _worker(await gate.admit(request))
        body = await _parse(request, RecentTasksQuery)
        await usecases.verify_lease(scope, task_id, body.lease_id)
        return _json(await _diagnostics(services).recent_tasks(body))

    @app.post(routes.DIAG_TASK, responses={200: {"model": TaskDetail}})
    @requires("runtime", "dev-agent")
    async def diagnose_task_detail(task_id: uuid.UUID, request: Request) -> Response:
        scope = _worker(await gate.admit(request))
        body = await _parse(request, TaskDetailQuery)
        await usecases.verify_lease(scope, task_id, body.lease_id)
        return _json(await _diagnostics(services).task_detail(body))

    @app.post(routes.DIAG_DEPLOYMENTS, responses={200: {"model": Deployments}})
    @requires("runtime", "dev-agent")
    async def diagnose_deployments(task_id: uuid.UUID, request: Request) -> Response:
        scope = _worker(await gate.admit(request))
        body = await _parse(request, DeploymentsQuery)
        await usecases.verify_lease(scope, task_id, body.lease_id)
        return _json(await _diagnostics(services).deployments(body))

    @app.post(routes.DIAG_LOGS, responses={200: {"model": LogLines}})
    @requires("runtime", "dev-agent")
    async def diagnose_logs(task_id: uuid.UUID, request: Request) -> Response:
        scope = _worker(await gate.admit(request))
        body = await _parse(request, LogsQuery)
        await usecases.verify_lease(scope, task_id, body.lease_id)
        return _json(await _diagnostics(services).logs(body))

    @app.post(routes.DIAG_PULLS, responses={200: {"model": Pulls}})
    @requires("runtime", "dev-agent")
    async def diagnose_pulls(task_id: uuid.UUID, request: Request) -> Response:
        scope = _worker(await gate.admit(request))
        body = await _parse(request, PullsQuery)
        await usecases.verify_lease(scope, task_id, body.lease_id)
        return _json(await _diagnostics(services).pulls(body))

    @app.post(routes.MEMORY_REMEMBER, responses={200: {"model": MemoryWritten}})
    @requires("runtime", "dev-agent")
    async def remember(task_id: uuid.UUID, request: Request) -> Response:
        scope = _worker(await gate.admit(request))
        body = await _parse(request, RememberRequest)
        await usecases.verify_lease(scope, task_id, body.lease_id)
        return _json(await _memory(services).remember(task_id, body))

    @app.post(routes.MEMORY_UPDATE, responses={200: {"model": MemoryWritten}})
    @requires("runtime", "dev-agent")
    async def update_memory(task_id: uuid.UUID, request: Request) -> Response:
        scope = _worker(await gate.admit(request))
        body = await _parse(request, UpdateMemoryRequest)
        await usecases.verify_lease(scope, task_id, body.lease_id)
        return _json(await _memory(services).update(task_id, body))

    @app.post(routes.MEMORY_FORGET, responses={200: {"model": MemoryWritten}})
    @requires("runtime", "dev-agent")
    async def forget_memory(task_id: uuid.UUID, request: Request) -> Response:
        scope = _worker(await gate.admit(request))
        body = await _parse(request, ForgetRequest)
        await usecases.verify_lease(scope, task_id, body.lease_id)
        return _json(await _memory(services).forget(task_id, body))

    @app.post(routes.MEMORY_SEARCH, responses={200: {"model": MemoryMatches}})
    @requires("runtime", "dev-agent")
    async def search_memory(task_id: uuid.UUID, request: Request) -> Response:
        scope = _worker(await gate.admit(request))
        body = await _parse(request, MemorySearch)
        await usecases.verify_lease(scope, task_id, body.lease_id)
        return _json(await _memory(services).search(body))

    @app.post(routes.PROMPT_CANDIDATES, responses={200: {"model": PromptCandidateCreated}})
    @requires("runtime")
    async def propose_prompt(task_id: uuid.UUID, request: Request) -> Response:
        role = _runtime(await gate.admit(request))
        body = await _parse(request, PromptCandidateRequest)
        await usecases.verify_lease(WorkerScope.shared(role.engine), task_id, body.lease_id)
        return _json(await _prompts(services).propose(task_id, body))

    @app.post(routes.PULL_SUBMISSIONS, responses={200: {"model": PullSubmitted}})
    @requires("runtime")
    async def submit_pull(task_id: uuid.UUID, request: Request) -> Response:
        role = _runtime(await gate.admit(request))
        body = await _parse(request, PullSubmission)
        await usecases.verify_lease(WorkerScope.shared(role.engine), task_id, body.lease_id)
        return _json(await _submissions(services).submit(task_id, body))

    @app.post(routes.PROMPT_READ, responses={200: {"model": PromptRead}})
    @requires("runtime")
    async def read_prompt(task_id: uuid.UUID, request: Request) -> Response:
        role = _runtime(await gate.admit(request))
        body = await _parse(request, PromptReadRequest)
        await usecases.verify_lease(WorkerScope.shared(role.engine), task_id, body.lease_id)
        return _json(await _prompts(services).read(task_id, body))

    @app.post(routes.DIGESTS, responses={200: {"model": DigestAccepted}})
    @requires("runtime")
    async def request_digest(task_id: uuid.UUID, request: Request) -> Response:
        role = _runtime(await gate.admit(request))
        body = await _parse(request, DigestRequest)
        await usecases.verify_lease(WorkerScope.shared(role.engine), task_id, body.lease_id)
        return _json(await _digests(services).request(task_id, body))

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

    @app.post(routes.TASK_DELEGATE, responses={200: {"model": DelegationAccepted}})
    @requires("runtime", "dev-agent")
    async def delegate(task_id: uuid.UUID, request: Request) -> Response:
        scope = _worker(await gate.admit(request))
        body = await _parse(request, DelegateRequest)
        return _json(await usecases.delegate(scope, task_id, body))

    @app.post(routes.TASK_HEARTBEAT, responses={200: {"model": LeaseExtended}})
    @requires("runtime", "dev-agent")
    async def heartbeat(task_id: uuid.UUID, request: Request) -> Response:
        scope = _worker(await gate.admit(request))
        beat = await _parse(request, Heartbeat)
        return _json(await usecases.heartbeat(scope, task_id, beat))

    @app.post(routes.TASK_RESULT, responses={200: {"model": ResultAccepted}})
    @requires("runtime", "dev-agent")
    async def report_result(task_id: uuid.UUID, request: Request) -> Response:
        scope = _worker(await gate.admit(request))
        report = await _parse(request, ResultReport)
        return _json(await usecases.report_result(scope, task_id, report))

    @app.post(routes.TRANSCRIPT_APPEND, responses={200: {"model": TranscriptAppended}})
    @requires("runtime", "dev-agent")
    async def append_transcript(task_id: uuid.UUID, request: Request) -> Response:
        scope = _worker(await gate.admit(request))
        body = await _parse(request, TranscriptAppend)
        return _json(await usecases.append_transcript(scope, task_id, body))

    @app.post(routes.TRANSCRIPT_LOAD, responses={200: {"model": TranscriptLoaded}})
    @requires("runtime", "dev-agent")
    async def load_transcript(task_id: uuid.UUID, request: Request) -> Response:
        scope = _worker(await gate.admit(request))
        query = await _parse(request, TranscriptQuery)
        return _json(await usecases.load_transcript(scope, task_id, query))

    @app.post(routes.TRANSCRIPT_SUBPATHS, responses={200: {"model": TranscriptSubpaths}})
    @requires("runtime", "dev-agent")
    async def transcript_subpaths(task_id: uuid.UUID, request: Request) -> Response:
        scope = _worker(await gate.admit(request))
        query = await _parse(request, SubpathQuery)
        return _json(await usecases.transcript_subpaths(scope, task_id, query))

    @app.post(routes.CREDENTIALS, responses={200: {"model": Credentials}})
    @requires("connector", "runtime", "dev-agent")
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

    @app.post(routes.GITHUB_TOKEN, responses={200: {"model": GitHubToken}})
    @requires("dev-agent")
    async def github_token(request: Request) -> Response:
        _dev_agent(await gate.admit(request))
        wanted = await _parse(request, GitHubTokenRequest)
        await usecases.verify_lease(WorkerScope.isolated(), wanted.task_id, wanted.lease_id)
        response = _json(await services.dev_tokens.issue(wanted.repository))
        response.headers["Cache-Control"] = "no-store"
        return response

    verify_declared_roles(app)
    return app


def verify_declared_roles(app: FastAPI) -> None:
    """Fail app creation when an endpoint other than health declares no role (I-28)."""
    undeclared = [
        route.path
        for route in app.routes
        if isinstance(route, APIRoute)
        and route.path != routes.HEALTH
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


def _dev_agent(principal: Principal) -> DevAgentRole:
    if not isinstance(principal.role, DevAgentRole):
        raise PermissionDeniedError("dev-agent role required")
    return principal.role


def _worker(principal: Principal) -> WorkerScope:
    """Shared runtimes see their engine's shared tasks; dev agents see isolated ones."""
    if isinstance(principal.role, RuntimeRole):
        return WorkerScope.shared(principal.role.engine)
    if isinstance(principal.role, DevAgentRole):
        return WorkerScope.isolated()
    raise PermissionDeniedError("worker role required")


def _runtime(principal: Principal) -> RuntimeRole:
    if not isinstance(principal.role, RuntimeRole):
        raise PermissionDeniedError("runtime role required")
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


def _prompts(services: CoreServices) -> PromptService:
    if services.prompts is None:
        raise DependencyUnavailableError("prompt candidates are not enabled in core")
    return services.prompts


def _submissions(services: CoreServices) -> PullSubmissions:
    if services.submissions is None:
        raise DependencyUnavailableError("pull request submission is not enabled in core")
    return services.submissions


def _digests(services: CoreServices) -> DigestService:
    if services.digests is None:
        raise DependencyUnavailableError("conversation summaries are not enabled")
    return services.digests


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


async def _digest_forever(services: CoreServices) -> None:
    digests = services.digests
    if digests is None:
        return
    while True:
        try:
            await digests.run_once()
        except Exception:
            _log.exception("conversation summary iteration failed")
        await asyncio.sleep(services.digest_interval_seconds)


async def _dispatch_forever(services: CoreServices) -> None:
    dispatcher = services.dispatcher
    if dispatcher is None:
        return
    while True:
        try:
            await dispatcher.dispatch_once()
        except Exception:
            _log.exception("dev Job dispatch iteration failed")
        await asyncio.sleep(services.dispatch_interval_seconds)


async def _reap_forever(services: CoreServices) -> None:
    while True:
        try:
            await services.usecases.reap_expired_leases()
        except Exception:
            _log.exception("lease reaper iteration failed")
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
