from __future__ import annotations

import base64
import uuid
from typing import TYPE_CHECKING

import httpx
import pytest
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature
from pydantic import BaseModel, SecretStr

from enikk_contracts.agents import McpToolGrant, ToolPolicy
from enikk_contracts.mcp import McpJwks
from enikk_contracts.routes import MCP_JWKS
from enikk_core.api import create_app
from enikk_core.mcp_tokens import McpTokenIssuer, granted_tools
from enikk_core_domain.testing import T0, agent

if TYPE_CHECKING:
    from conftest import Harness

    from enikk_contracts.agents import AgentDefinition
    from enikk_contracts.mcp import McpJwk

TASK = uuid.UUID(int=1)
LEASE = uuid.UUID(int=2)
SESSION = uuid.UUID(int=3)


class Claims(BaseModel):
    iss: str
    aud: str
    sub: str
    iat: int
    exp: int
    task: str
    lease: str
    agent: str
    session: str
    tools: list[str]


def pem(key: ec.EllipticCurvePrivateKey) -> SecretStr:
    raw = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    return SecretStr(raw.decode())


def with_mcp(*grants: McpToolGrant) -> AgentDefinition:
    return agent("enikk").model_copy(update={"tools": ToolPolicy(mcp=grants)})


def unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def verify(token: str, jwk: McpJwk) -> Claims:
    """Check the ES256 signature with the published key, as the gateway does."""
    head, body, sig = token.split(".")
    numbers = ec.EllipticCurvePublicNumbers(
        int.from_bytes(unb64(jwk.x), "big"), int.from_bytes(unb64(jwk.y), "big"), ec.SECP256R1()
    )
    raw = unb64(sig)
    der = encode_dss_signature(int.from_bytes(raw[:32], "big"), int.from_bytes(raw[32:], "big"))
    numbers.public_key().verify(der, f"{head}.{body}".encode(), ec.ECDSA(hashes.SHA256()))
    return Claims.model_validate_json(unb64(body))


def issue(issuer: McpTokenIssuer, definition: AgentDefinition) -> SecretStr | None:
    return issuer.issue(
        task_id=TASK, lease_id=LEASE, session_id=SESSION, definition=definition, now=T0
    )


def test_token_names_the_task_and_only_the_granted_tools() -> None:
    key = ec.generate_private_key(ec.SECP256R1())
    issuer = McpTokenIssuer(lambda: pem(key))
    definition = with_mcp(McpToolGrant(server="scheduler", allow=("list_schedules", "list_runs")))

    token = issue(issuer, definition)

    assert token is not None
    (jwk,) = issuer.jwks().keys
    claims = verify(token.get_secret_value(), jwk)
    assert claims.iss == "enikk-core"
    assert claims.aud == "enikk-agentgateway"
    assert (claims.task, claims.lease, claims.session) == (str(TASK), str(LEASE), str(SESSION))
    assert claims.agent == "enikk"
    assert claims.tools == ["scheduler:list_schedules", "scheduler:list_runs"]
    assert claims.exp - claims.iat == definition.limits.timeout_seconds + 300


def test_signature_does_not_verify_with_another_key() -> None:
    issuer = McpTokenIssuer(lambda: pem(ec.generate_private_key(ec.SECP256R1())))
    other = McpTokenIssuer(lambda: pem(ec.generate_private_key(ec.SECP256R1())))
    token = issue(issuer, with_mcp(McpToolGrant(server="scheduler", allow=("list_schedules",))))
    assert token is not None
    try:
        verify(token.get_secret_value(), other.jwks().keys[0])
    except InvalidSignature:
        return
    raise AssertionError("verified with the wrong key")


def test_no_token_without_grants_or_key() -> None:
    key = ec.generate_private_key(ec.SECP256R1())
    granted = with_mcp(McpToolGrant(server="scheduler", allow=("list_schedules",)))

    assert issue(McpTokenIssuer(lambda: pem(key)), agent("enikk")) is None
    assert issue(McpTokenIssuer(lambda: None), granted) is None
    assert issue(McpTokenIssuer(lambda: SecretStr("not a key")), granted) is None
    rsa_like = ec.generate_private_key(ec.SECP384R1())
    assert issue(McpTokenIssuer(lambda: pem(rsa_like)), granted) is None
    assert McpTokenIssuer(lambda: None).jwks() == McpJwks()


def test_granted_tools_are_server_qualified() -> None:
    definition = with_mcp(
        McpToolGrant(server="scheduler", allow=("get_schedule",)),
        McpToolGrant(server="other", allow=("x",)),
    )
    assert granted_tools(definition) == ("scheduler:get_schedule", "other:x")


@pytest.mark.anyio
async def test_jwks_is_served_without_a_token(harness: Harness) -> None:
    key = ec.generate_private_key(ec.SECP256R1())
    harness.services.mcp_tokens = McpTokenIssuer(lambda: pem(key))
    app = create_app(harness.services, run_reaper=False)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://core.test"
    ) as client:
        response = await client.get(MCP_JWKS)
    assert response.status_code == 200
    jwks = McpJwks.model_validate_json(response.content)
    assert len(jwks.keys) == 1
    assert '"d":' not in response.text


@pytest.mark.anyio
async def test_jwks_is_unavailable_without_a_key(harness: Harness) -> None:
    harness.services.mcp_tokens = McpTokenIssuer(lambda: None)
    app = create_app(harness.services, run_reaper=False)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://core.test"
    ) as client:
        response = await client.get(MCP_JWKS)
    assert response.status_code == 503
