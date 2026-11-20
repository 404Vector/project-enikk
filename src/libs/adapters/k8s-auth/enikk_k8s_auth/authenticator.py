"""``Authenticator`` backed by the Kubernetes TokenReview API, with a short result cache."""

from __future__ import annotations

import hashlib
import logging
import ssl
from collections import OrderedDict
from datetime import timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Final

import httpx
from pydantic import Field, ValidationError

from enikk_contracts.routes import AUDIENCE
from enikk_core_domain.auth import ServiceIdentity
from enikk_core_domain.errors import AuthenticationError, DependencyUnavailableError
from enikk_k8s_auth.api import TokenReviewRequest, TokenReviewResponse, TokenReviewSpec
from enikk_kernel.models import FrozenModel

if TYPE_CHECKING:
    from collections.abc import Generator
    from datetime import datetime

    from enikk_kernel.clock import Clock

_log = logging.getLogger(__name__)

SERVICE_ACCOUNT_DIR: Final = Path("/var/run/secrets/kubernetes.io/serviceaccount")
TOKEN_REVIEW_PATH: Final = "/apis/authentication.k8s.io/v1/tokenreviews"  # noqa: S105 - URL path
SA_USERNAME_PREFIX: Final = "system:serviceaccount:"


class TokenReviewConfig(FrozenModel):
    api_url: str = "https://kubernetes.default.svc"
    ca_file: Path = SERVICE_ACCOUNT_DIR / "ca.crt"
    token_file: Path = SERVICE_ACCOUNT_DIR / "token"
    audience: str = AUDIENCE
    cache_ttl: timedelta = timedelta(seconds=90)
    cache_size: Annotated[int, Field(ge=1, le=10_000)] = 256
    timeout_seconds: Annotated[float, Field(gt=0, le=30)] = 5.0


class FileBearerAuth(httpx.Auth):
    """Sends the token stored in ``path``, re-read on every request (kubelet rotates it)."""

    def __init__(self, path: Path) -> None:
        self._path = path

    def auth_flow(self, request: httpx.Request) -> Generator[httpx.Request, httpx.Response, None]:
        request.headers["Authorization"] = f"Bearer {self._path.read_text().strip()}"
        yield request


class _TtlCache:
    def __init__(self, ttl: timedelta, size: int) -> None:
        self._ttl = ttl
        self._size = size
        self._entries: OrderedDict[str, tuple[ServiceIdentity, datetime]] = OrderedDict()

    def get(self, key: str, now: datetime) -> ServiceIdentity | None:
        entry = self._entries.get(key)
        if entry is None:
            return None
        identity, expires_at = entry
        if now >= expires_at:
            del self._entries[key]
            return None
        return identity

    def put(self, key: str, identity: ServiceIdentity, now: datetime) -> None:
        self._entries[key] = (identity, now + self._ttl)
        self._entries.move_to_end(key)
        while len(self._entries) > self._size:
            self._entries.popitem(last=False)


def parse_service_account(username: str) -> ServiceIdentity | None:
    if not username.startswith(SA_USERNAME_PREFIX):
        return None
    namespace, _, name = username.removeprefix(SA_USERNAME_PREFIX).partition(":")
    try:
        return ServiceIdentity(namespace=namespace, name=name)
    except ValidationError:
        return None


class TokenReviewAuthenticator:
    """Verifies ServiceAccount tokens for one audience. Only successes are cached."""

    def __init__(self, http: httpx.AsyncClient, config: TokenReviewConfig, clock: Clock) -> None:
        self._http = http
        self._config = config
        self._clock = clock
        self._cache = _TtlCache(config.cache_ttl, config.cache_size)

    @classmethod
    def in_cluster(cls, config: TokenReviewConfig, clock: Clock) -> TokenReviewAuthenticator:
        http = httpx.AsyncClient(
            base_url=config.api_url,
            verify=ssl.create_default_context(cafile=config.ca_file),
            auth=FileBearerAuth(config.token_file),
            timeout=config.timeout_seconds,
        )
        return cls(http, config, clock)

    async def aclose(self) -> None:
        await self._http.aclose()

    async def authenticate(self, token: str) -> ServiceIdentity:
        if not token:
            raise AuthenticationError("missing bearer token")
        key = hashlib.sha256(token.encode()).hexdigest()
        now = self._clock.now()
        cached = self._cache.get(key, now)
        if cached is not None:
            return cached
        identity = await self._review(token)
        self._cache.put(key, identity, now)
        return identity

    async def _review(self, token: str) -> ServiceIdentity:
        request = TokenReviewRequest(
            spec=TokenReviewSpec(token=token, audiences=(self._config.audience,))
        )
        try:
            response = await self._http.post(
                TOKEN_REVIEW_PATH,
                content=request.model_dump_json(by_alias=True),
                headers={"Content-Type": "application/json"},
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise DependencyUnavailableError(f"TokenReview request failed: {exc}") from exc
        try:
            review = TokenReviewResponse.model_validate_json(response.content)
        except ValidationError as exc:
            raise DependencyUnavailableError("TokenReview returned an unexpected body") from exc

        status = review.status
        if not status.authenticated:
            _log.info("token rejected: %s", status.error or "not authenticated")
            raise AuthenticationError("token rejected")
        if self._config.audience not in status.audiences:
            raise AuthenticationError("token audience mismatch")
        identity = parse_service_account(status.user.username)
        if identity is None:
            raise AuthenticationError("token does not belong to a ServiceAccount")
        return identity
