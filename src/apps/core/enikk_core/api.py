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
from enikk_contracts.common import ConnectorKind
from enikk_contracts.credentials import CredentialRequest, Credentials
from enikk_contracts.errors import ErrorCode, ErrorResponse
from enikk_contracts.events import EventAccepted, InboundEvent
from enikk_contracts.github import GitHubHealth, GitHubToken, GitHubTokenRequest
from enikk_contracts.health import HealthResponse
from enikk_contracts.outbox import OutboxAck, OutboxAcked, OutboxBatch, OutboxPoll
from enikk_contracts.tasks import (
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
    NotFoundError,
    PermissionDeniedError,
    UnknownAgentError,
)
from enikk_core_domain.model import WorkerScope
from enikk_kernel.errors import ConfigurationError

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable

    from enikk_contracts.base import Contract
    from enikk_core.credentials import CredentialIssuer
    from enikk_core.dispatcher import DevJobDispatcher
    from enikk_core.github import DevTokenIssuer, GitHubHealthCheck
    from enikk_core.security import Gatekeeper
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


def create_app(services: CoreServices, *, run_reaper: bool = True) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        loops: list[asyncio.Task[None]] = []
        if run_reaper:
            loops.append(asyncio.create_task(_reap_forever(services)))
            if services.dispatcher is not None:
                loops.append(asyncio.create_task(_dispatch_forever(services)))
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
