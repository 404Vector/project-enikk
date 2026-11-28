"""Core's task tokens, checked again by the Drive server (ADR-0052 §4, as ADR-0044 §5).

agentgateway checks the token and copies it into ``x-enikk-mcp-token``. The server checks
it once more with core's public keys (``/v1/mcp/jwks``), so a caller that reaches the
server's Service without the gateway gets nothing. A tool call must also be one the token
names (``drive:<tool>``), and the calling session is the token's.
"""

from __future__ import annotations

import base64
import binascii
import logging
import time
from typing import TYPE_CHECKING, Final

import httpx
import pydantic
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature

from enikk_contracts.mcp import TOKEN_AUDIENCE, TOKEN_ISSUER, McpJwks
from enikk_kernel.errors import EnikkError

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

TOKEN_HEADER: Final = "x-enikk-mcp-token"  # noqa: S105 - a header name
"""Set by the gateway from the caller's ``Authorization`` header."""
SERVER: Final = "drive"
_BEARER: Final = "bearer"
_COORDINATE_BYTES: Final = 32
_LEEWAY_SECONDS: Final = 30
_REFRESH_SECONDS: Final = 30.0
"""At most one key fetch per this many seconds for an unknown key id."""

_log = logging.getLogger(__name__)


class TokenRejectedError(EnikkError):
    """The request carries no valid task token, or the token does not name the tool."""


class _Header(pydantic.BaseModel):
    alg: str
    kid: str


class TaskClaims(pydantic.BaseModel):
    iss: str
    aud: str
    exp: int
    task: str
    session: str
    tools: list[str]

    def allows(self, tool: str) -> bool:
        return f"{SERVER}:{tool}" in self.tools


class TokenVerifier:
    """Checks ES256 task tokens with core's public keys, fetched and cached."""

    def __init__(
        self,
        http: httpx.AsyncClient,
        jwks_url: str,
        *,
        clock: Callable[[], float] = time.time,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._http = http
        self._url = jwks_url
        self._clock = clock
        self._monotonic = monotonic
        self._keys: dict[str, ec.EllipticCurvePublicKey] = {}
        self._fetched_at: float | None = None

    async def verify(self, headers: Mapping[str, str] | None) -> TaskClaims:
        token = _token(headers)
        parts = token.split(".")
        if len(parts) != 3:
            raise TokenRejectedError("malformed task token")
        try:
            header = _Header.model_validate_json(_b64decode(parts[0]))
            claims = TaskClaims.model_validate_json(_b64decode(parts[1]))
            signature = _b64decode(parts[2])
        except (ValueError, pydantic.ValidationError) as exc:
            raise TokenRejectedError("malformed task token") from exc
        if header.alg != "ES256" or len(signature) != 2 * _COORDINATE_BYTES:
            raise TokenRejectedError("task token is not ES256")
        key = await self._key(header.kid)
        der = encode_dss_signature(
            int.from_bytes(signature[:_COORDINATE_BYTES], "big"),
            int.from_bytes(signature[_COORDINATE_BYTES:], "big"),
        )
        try:
            key.verify(der, f"{parts[0]}.{parts[1]}".encode(), ec.ECDSA(hashes.SHA256()))
        except InvalidSignature as exc:
            raise TokenRejectedError("task token signature is invalid") from exc
        if claims.iss != TOKEN_ISSUER or claims.aud != TOKEN_AUDIENCE:
            raise TokenRejectedError("task token is not core's token for the gateway")
        if claims.exp + _LEEWAY_SECONDS < self._clock():
            raise TokenRejectedError("task token has expired")
        return claims

    async def _key(self, kid: str) -> ec.EllipticCurvePublicKey:
        if kid not in self._keys:
            now = self._monotonic()
            if self._fetched_at is None or now - self._fetched_at >= _REFRESH_SECONDS:
                self._fetched_at = now
                self._keys = await self._fetch()
        key = self._keys.get(kid)
        if key is None:
            raise TokenRejectedError("task token is signed with an unknown key")
        return key

    async def _fetch(self) -> dict[str, ec.EllipticCurvePublicKey]:
        try:
            response = await self._http.get(self._url)
            response.raise_for_status()
            jwks = McpJwks.model_validate_json(response.content)
        except (httpx.HTTPError, pydantic.ValidationError) as exc:
            _log.warning("fetching core's MCP token keys failed: %s", exc)
            return self._keys
        keys: dict[str, ec.EllipticCurvePublicKey] = {}
        for jwk in jwks.keys:
            numbers = ec.EllipticCurvePublicNumbers(
                int.from_bytes(_b64decode(jwk.x), "big"),
                int.from_bytes(_b64decode(jwk.y), "big"),
                ec.SECP256R1(),
            )
            keys[jwk.kid] = numbers.public_key()
        return keys


def _token(headers: Mapping[str, str] | None) -> str:
    lowered = {key.lower(): value for key, value in (headers or {}).items()}
    value = lowered.get(TOKEN_HEADER, "").strip()
    scheme, _, rest = value.partition(" ")
    if scheme.lower() == _BEARER:
        value = rest.strip()
    elif value.lower() == _BEARER:
        value = ""
    if not value:
        raise TokenRejectedError(f"the request carries no task token ({TOKEN_HEADER})")
    return value


def _b64decode(text: str) -> bytes:
    try:
        return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    except (binascii.Error, ValueError) as exc:
        raise ValueError("not base64url") from exc
