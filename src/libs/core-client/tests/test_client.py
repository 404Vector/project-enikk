from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

import httpx
import pytest
from pydantic import SecretStr

from enikk_contracts import routes
from enikk_contracts.common import ConnectorKind, ConversationRef
from enikk_contracts.errors import ErrorCode, ErrorResponse
from enikk_contracts.events import EventAccepted, InboundEvent
from enikk_contracts.tasks import LeaseRequest, LeaseResponse
from enikk_core_client import (
    CoreApiError,
    CoreClient,
    CoreConflictError,
    CoreUnauthenticatedError,
    CoreUnavailableError,
    FileTokenSource,
    StaticTokenSource,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

pytestmark = pytest.mark.anyio

EVENT = InboundEvent(
    event_id="Ev1",
    conversation=ConversationRef(connector=ConnectorKind.SLACK, channel="C1", thread="1.0"),
    user="U1",
    text="hello",
    agent="assistant",
)


def _client(handler: Callable[[httpx.Request], httpx.Response]) -> CoreClient:
    return CoreClient.connect(
        "http://core.test",
        StaticTokenSource(SecretStr("sa-token")),
        transport=httpx.MockTransport(handler),
    )


async def test_submit_event_sends_contract_and_token() -> None:
    accepted = EventAccepted(
        task_id=uuid.uuid4(), session_id=uuid.uuid4(), agent="assistant", duplicate=False
    )
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, text=accepted.model_dump_json())

    async with _client(handler) as client:
        assert await client.submit_event(EVENT) == accepted
    assert seen[0].url.path == routes.EVENTS
    assert seen[0].headers["Authorization"] == "Bearer sa-token"
    assert InboundEvent.model_validate_json(seen[0].content) == EVENT


async def test_long_poll_timeout_exceeds_wait() -> None:
    timeouts: list[object] = []

    def handler(request: httpx.Request) -> httpx.Response:
        timeouts.append(request.extensions["timeout"]["read"])
        return httpx.Response(200, text=LeaseResponse(assignment=None).model_dump_json())

    async with _client(handler) as client:
        response = await client.lease_task(LeaseRequest(wait_seconds=20))
    assert response.assignment is None
    assert timeouts == [30.0]


@pytest.mark.parametrize(
    ("status", "code", "expected"),
    [
        (401, ErrorCode.UNAUTHENTICATED, CoreUnauthenticatedError),
        (409, ErrorCode.CONFLICT, CoreConflictError),
        (503, ErrorCode.UNAVAILABLE, CoreUnavailableError),
    ],
)
async def test_error_bodies_map_to_exceptions(
    status: int, code: ErrorCode, expected: type[CoreApiError]
) -> None:
    body = ErrorResponse(code=code, message="nope").model_dump_json()
    async with _client(lambda _: httpx.Response(status, text=body)) as client:
        with pytest.raises(expected, match="nope"):
            await client.submit_event(EVENT)


async def test_unparseable_errors_and_transport_failures() -> None:
    async with _client(lambda _: httpx.Response(502, text="bad gateway")) as client:
        with pytest.raises(CoreUnavailableError):
            await client.submit_event(EVENT)

    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    async with _client(refuse) as client:
        with pytest.raises(CoreUnavailableError):
            await client.submit_event(EVENT)


async def test_unexpected_success_body_is_an_error() -> None:
    async with _client(lambda _: httpx.Response(200, text='{"weird": 1}')) as client:
        with pytest.raises(CoreApiError, match="EventAccepted"):
            await client.submit_event(EVENT)


def test_file_token_source_rereads(tmp_path: Path) -> None:
    path = tmp_path / "token"
    source = FileTokenSource(path)
    path.write_text("one\n")
    assert source.token() == "one"
    path.write_text("two")
    assert source.token() == "two"
