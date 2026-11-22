"""GitHub App client: app JWTs and repository-scoped installation tokens (ADR-0019).

The private key is read through a callable on every use, so a rotated key is picked
up without a restart. Neither the key nor issued tokens are ever logged.
"""

from __future__ import annotations

import contextlib
import logging
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Annotated, Final, Self

import httpx
import jwt
from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError

from enikk_core_domain.errors import DependencyUnavailableError
from enikk_github.permissions import Access, TokenPurpose, permissions_for
from enikk_kernel.models import FrozenModel

if TYPE_CHECKING:
    from collections.abc import Callable

    from enikk_kernel.clock import Clock

_log = logging.getLogger(__name__)

API_VERSION: Final = "2022-11-28"
JWT_BACKDATE: Final = timedelta(seconds=60)
JWT_LIFETIME: Final = timedelta(minutes=9)

RepositoryName = Annotated[str, Field(pattern=r"^[A-Za-z0-9._-]{1,100}$")]
AccountName = Annotated[str, Field(pattern=r"^[A-Za-z0-9-]{1,39}$")]


class GitHubAppConfig(FrozenModel):
    app_id: Annotated[int, Field(gt=0)]
    installation_id: Annotated[int, Field(gt=0)]
    owner: AccountName
    api_url: str = "https://api.github.com"
    timeout_seconds: Annotated[float, Field(gt=0, le=60)] = 10.0


class InstallationToken(FrozenModel):
    token: SecretStr
    expires_at: datetime
    repository: RepositoryName
    permissions: dict[str, str]


class _AppClaims(FrozenModel):
    iat: int
    exp: int
    iss: str


class _AccessTokenRequest(FrozenModel):
    repositories: tuple[RepositoryName, ...]
    permissions: dict[str, Access]


class _GitHubBody(BaseModel):
    """Responses from GitHub: unknown fields are ignored."""

    model_config = ConfigDict(frozen=True, extra="ignore")


class _AccessTokenResponse(_GitHubBody):
    token: SecretStr
    expires_at: datetime
    permissions: dict[str, str] = {}


class _InstallationResponse(_GitHubBody):
    permissions: dict[str, str] = {}


class _ErrorBody(_GitHubBody):
    message: str = ""


class GitHubApp:
    def __init__(
        self,
        http: httpx.AsyncClient,
        config: GitHubAppConfig,
        private_key: Callable[[], SecretStr | None],
        clock: Clock,
    ) -> None:
        self._http = http
        self._config = config
        self._private_key = private_key
        self._clock = clock

    @classmethod
    def create(
        cls,
        config: GitHubAppConfig,
        private_key: Callable[[], SecretStr | None],
        clock: Clock,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> Self:
        http = httpx.AsyncClient(
            base_url=config.api_url,
            timeout=config.timeout_seconds,
            transport=transport,
            headers={
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": API_VERSION,
                "User-Agent": "enikk-core",
            },
        )
        return cls(http, config, private_key, clock)

    @property
    def config(self) -> GitHubAppConfig:
        return self._config

    async def aclose(self) -> None:
        await self._http.aclose()

    async def installation_permissions(self) -> dict[str, str]:
        """Permissions the owner granted to the installation."""
        path = f"/app/installations/{self._config.installation_id}"
        response = await self._request("GET", path, bearer=self._app_jwt())
        return _parse(_InstallationResponse, response).permissions

    async def installation_token(
        self, repository: str, purpose: TokenPurpose, *, workflows: bool = False
    ) -> InstallationToken:
        """A token for exactly one repository with the fixed permissions of ``purpose``."""
        request = _AccessTokenRequest(
            repositories=(repository,), permissions=permissions_for(purpose, workflows=workflows)
        )
        path = f"/app/installations/{self._config.installation_id}/access_tokens"
        response = await self._request(
            "POST", path, bearer=self._app_jwt(), body=request.model_dump_json()
        )
        issued = _parse(_AccessTokenResponse, response)
        _log.info("issued a %s token for %s until %s", purpose, repository, issued.expires_at)
        return InstallationToken(
            token=issued.token,
            expires_at=issued.expires_at,
            repository=repository,
            permissions=issued.permissions,
        )

    async def repository_reachable(self, token: InstallationToken) -> bool:
        path = f"/repos/{self._config.owner}/{token.repository}"
        try:
            await self._request("GET", path, bearer=token.token.get_secret_value())
        except DependencyUnavailableError:
            return False
        return True

    async def revoke(self, token: InstallationToken) -> None:
        """Best effort: an unrevoked token still expires within the hour."""
        try:
            await self._request(
                "DELETE", "/installation/token", bearer=token.token.get_secret_value()
            )
        except DependencyUnavailableError as exc:
            _log.warning("could not revoke a token for %s: %s", token.repository, exc)

    # ------------------------------------------------------------ internals

    def _app_jwt(self) -> str:
        key = self._private_key()
        if key is None:
            raise DependencyUnavailableError(
                "GitHub App private key is not registered (github.app-private-key)"
            )
        now = self._clock.now()
        claims = _AppClaims(
            iat=int((now - JWT_BACKDATE).timestamp()),
            exp=int((now + JWT_LIFETIME).timestamp()),
            iss=str(self._config.app_id),
        )
        try:
            return jwt.encode(claims.model_dump(), key.get_secret_value(), algorithm="RS256")
        except (ValueError, TypeError, jwt.PyJWTError) as exc:
            # The message of these errors never contains the key itself.
            raise DependencyUnavailableError(
                f"GitHub App private key cannot sign: {type(exc).__name__}"
            ) from None

    async def _request(
        self, method: str, path: str, *, bearer: str, body: str | None = None
    ) -> httpx.Response:
        headers = {"Authorization": f"Bearer {bearer}"}
        if body is not None:
            headers["Content-Type"] = "application/json"
        try:
            response = await self._http.request(method, path, content=body, headers=headers)
        except httpx.HTTPError as exc:
            raise DependencyUnavailableError(
                f"GitHub {method} {path} failed: {type(exc).__name__}"
            ) from None
        if response.is_success:
            return response
        message = ""
        with contextlib.suppress(ValidationError):
            message = _ErrorBody.model_validate_json(response.content).message
        raise DependencyUnavailableError(
            f"GitHub {method} {path} returned {response.status_code}: {message[:300]}"
        )


def _parse[T: BaseModel](model: type[T], response: httpx.Response) -> T:
    try:
        return model.model_validate_json(response.content)
    except ValidationError:
        raise DependencyUnavailableError(
            f"GitHub returned an unexpected {model.__name__}"
        ) from None
