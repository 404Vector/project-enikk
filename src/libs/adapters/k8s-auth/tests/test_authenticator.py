from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING

import httpx
import pytest

from enikk_core_domain.auth import ServiceIdentity
from enikk_core_domain.errors import AuthenticationError, DependencyUnavailableError
from enikk_k8s_auth.api import TokenReviewRequest
from enikk_k8s_auth.authenticator import (
    TOKEN_REVIEW_PATH,
    FileBearerAuth,
    TokenReviewAuthenticator,
    TokenReviewConfig,
    parse_service_account,
)
from enikk_kernel.testing import ManualClock

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.anyio

SLACK = ServiceIdentity(namespace="enikk", name="enikk-connector-slack")


def _review_body(
    *, authenticated: bool = True, username: str = "", audiences: str = '["enikk-core"]'
) -> str:
    return (
        '{"apiVersion": "authentication.k8s.io/v1", "kind": "TokenReview", "status": {'
        f'"authenticated": {"true" if authenticated else "false"},'
        f'"user": {{"username": "{username}", "uid": "u-1", "groups": ["g"]}},'
        f'"audiences": {audiences}}}}}'
    )


class _FakeApiServer:
    def __init__(self, body: str, status: int = 201) -> None:
        self.body = body
        self.status = status
        self.requests: list[TokenReviewRequest] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        assert request.url.path == TOKEN_REVIEW_PATH
        self.requests.append(TokenReviewRequest.model_validate_json(request.content))
        return httpx.Response(self.status, text=self.body)


def _authenticator(
    server: _FakeApiServer, clock: ManualClock | None = None
) -> TokenReviewAuthenticator:
    http = httpx.AsyncClient(
        base_url="https://k8s.test", transport=httpx.MockTransport(server.handle)
    )
    return TokenReviewAuthenticator(http, TokenReviewConfig(), clock or ManualClock())


async def test_valid_service_account_token_is_accepted() -> None:
    server = _FakeApiServer(
        _review_body(username="system:serviceaccount:enikk:enikk-connector-slack")
    )
    identity = await _authenticator(server).authenticate("token-a")
    assert identity == SLACK
    assert server.requests[0].spec.audiences == ("enikk-core",)
    assert server.requests[0].spec.token == "token-a"


async def test_results_are_cached_until_ttl() -> None:
    server = _FakeApiServer(
        _review_body(username="system:serviceaccount:enikk:enikk-connector-slack")
    )
    clock = ManualClock()
    auth = _authenticator(server, clock)
    await auth.authenticate("token-a")
    await auth.authenticate("token-a")
    assert len(server.requests) == 1
    clock.advance(timedelta(seconds=91))
    await auth.authenticate("token-a")
    assert len(server.requests) == 2


@pytest.mark.parametrize(
    "body",
    [
        _review_body(authenticated=False),
        _review_body(username="system:serviceaccount:enikk:x", audiences='["other"]'),
        _review_body(username="alice@example.com"),
    ],
    ids=["unauthenticated", "wrong-audience", "not-a-service-account"],
)
async def test_invalid_tokens_are_rejected_and_not_cached(body: str) -> None:
    server = _FakeApiServer(body)
    auth = _authenticator(server)
    for _ in range(2):
        with pytest.raises(AuthenticationError):
            await auth.authenticate("token-b")
    assert len(server.requests) == 2


async def test_empty_token_is_rejected_without_calling_the_api() -> None:
    server = _FakeApiServer(_review_body())
    with pytest.raises(AuthenticationError):
        await _authenticator(server).authenticate("")
    assert server.requests == []


async def test_api_failure_is_reported_as_unavailable() -> None:
    server = _FakeApiServer("{}", status=503)
    with pytest.raises(DependencyUnavailableError):
        await _authenticator(server).authenticate("token-c")


def test_parse_service_account() -> None:
    assert parse_service_account("system:serviceaccount:enikk:enikk-runtime-claude") == (
        ServiceIdentity(namespace="enikk", name="enikk-runtime-claude")
    )
    assert parse_service_account("system:serviceaccount:enikk") is None
    assert parse_service_account("system:node:worker") is None


def test_file_bearer_auth_rereads_token(tmp_path: Path) -> None:
    token_file = tmp_path / "token"
    auth = FileBearerAuth(token_file)
    token_file.write_text("first\n")
    request = next(auth.auth_flow(httpx.Request("GET", "https://k8s.test")))
    assert request.headers["Authorization"] == "Bearer first"
    token_file.write_text("second")
    request = next(auth.auth_flow(httpx.Request("GET", "https://k8s.test")))
    assert request.headers["Authorization"] == "Bearer second"
