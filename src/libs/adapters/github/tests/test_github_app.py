from __future__ import annotations

from typing import TYPE_CHECKING

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from pydantic import BaseModel, SecretStr

from enikk_core_domain.errors import DependencyUnavailableError
from enikk_github.app import GitHubApp, GitHubAppConfig
from enikk_github.permissions import (
    NEVER_FOR_DEVELOP,
    Access,
    TokenPurpose,
    permissions_for,
    required_permissions,
)
from enikk_kernel.testing import ManualClock

if TYPE_CHECKING:
    from collections.abc import Callable

CONFIG = GitHubAppConfig(app_id=5869950, installation_id=184545127, owner="404Vector")
ISSUED = "ghs_issued-token-value"


class _Sent(BaseModel):
    repositories: list[str]
    permissions: dict[str, str]


@pytest.fixture(scope="module")
def key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _pem(key: rsa.RSAPrivateKey) -> SecretStr:
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption(),
    )
    return SecretStr(pem.decode())


def _app(handler: Callable[[httpx.Request], httpx.Response], key: SecretStr | None) -> GitHubApp:
    return GitHubApp.create(
        CONFIG, lambda: key, ManualClock(), transport=httpx.MockTransport(handler)
    )


@pytest.mark.anyio
async def test_token_is_scoped_to_one_repository_and_the_purpose(key: rsa.RSAPrivateKey) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            201,
            json={
                "token": ISSUED,
                "expires_at": "2026-11-22T06:00:00Z",
                "permissions": {"contents": "write"},
            },
        )

    app = _app(handler, _pem(key))
    token = await app.installation_token("project-enikk", TokenPurpose.DEVELOP)

    request = seen[0]
    assert request.method == "POST"
    assert request.url.path == "/app/installations/184545127/access_tokens"
    sent = _Sent.model_validate_json(request.content)
    assert sent.repositories == ["project-enikk"]
    assert sent.permissions == permissions_for(TokenPurpose.DEVELOP)
    claims = jwt.decode(
        request.headers["Authorization"].removeprefix("Bearer "),
        key.public_key(),
        algorithms=["RS256"],
        options={"verify_exp": False, "verify_iat": False},
    )
    assert claims["iss"] == "5869950"
    assert claims["exp"] - claims["iat"] <= 600
    assert token.repository == "project-enikk"
    assert token.token.get_secret_value() == ISSUED
    assert ISSUED not in repr(token)


@pytest.mark.parametrize("workflows", [False, True])
def test_development_tokens_never_approve_or_administer(workflows: bool) -> None:
    permissions = permissions_for(TokenPurpose.DEVELOP, workflows=workflows)
    written = {name for name, access in permissions.items() if access is Access.WRITE}
    assert not written & NEVER_FOR_DEVELOP
    assert ("workflows" in permissions) is workflows


@pytest.mark.parametrize("purpose", [p for p in TokenPurpose if p is not TokenPurpose.DEVELOP])
def test_only_development_tokens_may_write_workflows(purpose: TokenPurpose) -> None:
    with pytest.raises(ValueError, match="workflows"):
        permissions_for(purpose, workflows=True)


def test_required_permissions_cover_every_purpose() -> None:
    required = required_permissions(workflows=False)
    assert required["statuses"] is Access.WRITE
    assert required["administration"] is Access.WRITE
    assert required["contents"] is Access.WRITE
    assert "workflows" not in required
    assert required_permissions(workflows=True)["workflows"] is Access.WRITE


@pytest.mark.anyio
async def test_missing_key_is_reported_without_calling_github() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("GitHub must not be called")

    app = _app(handler, None)
    with pytest.raises(DependencyUnavailableError, match="not registered"):
        await app.installation_permissions()


@pytest.mark.anyio
async def test_unusable_key_is_reported_without_its_contents() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("GitHub must not be called")

    app = _app(handler, SecretStr("not-a-pem-secret-text"))
    with pytest.raises(DependencyUnavailableError) as raised:
        await app.installation_permissions()
    assert "not-a-pem-secret-text" not in str(raised.value)


@pytest.mark.anyio
async def test_github_errors_become_dependency_errors(key: rsa.RSAPrivateKey) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(422, json={"message": "repository not in installation"})

    app = _app(handler, _pem(key))
    with pytest.raises(DependencyUnavailableError, match=r"422: repository not in installation"):
        await app.installation_token("elsewhere", TokenPurpose.WATCH)


@pytest.mark.anyio
async def test_reachability_and_revocation_use_the_issued_token(key: rsa.RSAPrivateKey) -> None:
    calls: list[tuple[str, str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path, request.headers["Authorization"]))
        if request.url.path.endswith("/access_tokens"):
            return httpx.Response(201, json={"token": ISSUED, "expires_at": "2026-11-22T06:00:00Z"})
        if request.url.path == "/repos/404Vector/missing":
            return httpx.Response(404, json={"message": "Not Found"})
        return httpx.Response(204 if request.method == "DELETE" else 200, json={})

    app = _app(handler, _pem(key))
    present = await app.installation_token("project-enikk", TokenPurpose.WATCH)
    missing = await app.installation_token("missing", TokenPurpose.WATCH)
    assert await app.repository_reachable(present)
    assert not await app.repository_reachable(missing)
    await app.revoke(present)
    token_calls = [c for c in calls if not c[1].endswith("/access_tokens")]
    assert token_calls == [
        ("GET", "/repos/404Vector/project-enikk", f"Bearer {ISSUED}"),
        ("GET", "/repos/404Vector/missing", f"Bearer {ISSUED}"),
        ("DELETE", "/installation/token", f"Bearer {ISSUED}"),
    ]


@pytest.mark.anyio
async def test_installation_permissions_are_read_with_the_app_jwt(key: rsa.RSAPrivateKey) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/app/installations/184545127"
        return httpx.Response(200, json={"permissions": {"contents": "write"}, "id": 1})

    app = _app(handler, _pem(key))
    assert await app.installation_permissions() == {"contents": "write"}
