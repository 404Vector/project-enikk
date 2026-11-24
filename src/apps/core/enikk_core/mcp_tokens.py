"""The tasks' MCP tokens (ADR-0039 §2).

Core signs one ES256 JWT per leased task. It names the task, its lease, the agent and
the MCP tools the definition grants (``<server>:<tool>``). The gateway checks the
signature with the public keys from ``/v1/mcp/jwks``, shows and allows only the named
tools, and hands the task and lease to the MCP server, which asks core whether the
lease is still alive.
"""

from __future__ import annotations

import base64
import hashlib
import logging
from typing import TYPE_CHECKING, Final

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
from pydantic import BaseModel, SecretStr

from enikk_contracts.mcp import TOKEN_AUDIENCE, TOKEN_ISSUER, McpJwk, McpJwks

if TYPE_CHECKING:
    import uuid
    from collections.abc import Callable
    from datetime import datetime

    from enikk_contracts.agents import AgentDefinition

TOKEN_MARGIN_SECONDS: Final = 300
"""How long a token outlives the task's own time limit."""
_COORDINATE_BYTES: Final = 32

_log = logging.getLogger(__name__)


class _Header(BaseModel):
    alg: str = "ES256"
    typ: str = "JWT"
    kid: str


class _Claims(BaseModel):
    iss: str = TOKEN_ISSUER
    aud: str = TOKEN_AUDIENCE
    sub: str
    iat: int
    exp: int
    task: str
    lease: str
    agent: str
    session: str
    tools: list[str]


def granted_tools(definition: AgentDefinition) -> tuple[str, ...]:
    """The definition's MCP tools as ``<server>:<tool>``."""
    return tuple(f"{grant.server}:{tool}" for grant in definition.tools.mcp for tool in grant.allow)


class McpTokenIssuer:
    """Signs the tasks' MCP tokens with the key from core's credentials."""

    def __init__(self, key: Callable[[], SecretStr | None]) -> None:
        self._key = key

    def issue(
        self,
        *,
        task_id: uuid.UUID,
        lease_id: uuid.UUID,
        session_id: uuid.UUID,
        definition: AgentDefinition,
        now: datetime,
    ) -> SecretStr | None:
        """``None`` when the definition grants no MCP tool or there is no key."""
        tools = granted_tools(definition)
        if not tools:
            return None
        key = self._private_key()
        if key is None:
            _log.warning("task %s: no MCP signing key; the task runs without MCP tools", task_id)
            return None
        issued = int(now.timestamp())
        claims = _Claims(
            sub=f"task:{task_id}",
            iat=issued,
            exp=issued + definition.limits.timeout_seconds + TOKEN_MARGIN_SECONDS,
            task=str(task_id),
            lease=str(lease_id),
            agent=definition.name,
            session=str(session_id),
            tools=list(tools),
        )
        header = _Header(kid=key_id(key.public_key()))
        signing_input = f"{_b64(header.model_dump_json())}.{_b64(claims.model_dump_json())}"
        der = key.sign(signing_input.encode(), ec.ECDSA(hashes.SHA256()))
        r, s = decode_dss_signature(der)
        signature = r.to_bytes(_COORDINATE_BYTES, "big") + s.to_bytes(_COORDINATE_BYTES, "big")
        return SecretStr(f"{signing_input}.{_b64(signature)}")

    def jwks(self) -> McpJwks:
        key = self._private_key()
        return McpJwks() if key is None else McpJwks(keys=(public_jwk(key.public_key()),))

    def _private_key(self) -> ec.EllipticCurvePrivateKey | None:
        value = self._key()
        if value is None:
            return None
        try:
            key = serialization.load_pem_private_key(value.get_secret_value().encode(), None)
        except (ValueError, TypeError):
            _log.error("the MCP signing key is not a PEM private key")
            return None
        if not isinstance(key, ec.EllipticCurvePrivateKey) or key.curve.name != "secp256r1":
            _log.error("the MCP signing key must be an EC P-256 key")
            return None
        return key


def public_jwk(key: ec.EllipticCurvePublicKey) -> McpJwk:
    numbers = key.public_numbers()
    return McpJwk(
        kid=key_id(key),
        x=_b64(numbers.x.to_bytes(_COORDINATE_BYTES, "big")),
        y=_b64(numbers.y.to_bytes(_COORDINATE_BYTES, "big")),
    )


def key_id(key: ec.EllipticCurvePublicKey) -> str:
    """A short digest of the public key."""
    der = key.public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    return _b64(hashlib.sha256(der).digest()[:12])


def _b64(data: str | bytes) -> str:
    raw = data.encode() if isinstance(data, str) else data
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()
