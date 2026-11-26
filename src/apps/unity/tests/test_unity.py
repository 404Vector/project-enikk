from __future__ import annotations

import base64
import time
from typing import TYPE_CHECKING

import httpx
import pydantic_core
import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
from pydantic import SecretStr
from starlette.testclient import TestClient

from enikk_contracts.mcp import TOKEN_AUDIENCE, TOKEN_ISSUER
from enikk_unity.app import HEALTH_PATH, MCP_PATH, create_app
from enikk_unity.relay import LIST_RESOURCES, READ_RESOURCE, Relay
from enikk_unity.tokens import TOKEN_HEADER, TokenRejectedError, TokenVerifier
from enikk_unity.upstream import (
    KEY_HEADER,
    SESSION_HEADER,
    UnityRequestError,
    UnityUnavailableError,
    UnityUpstream,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

pytestmark = pytest.mark.anyio

URL = "http://unity.test/mcp"
JWKS_URL = "http://core.test/v1/mcp/jwks"
KEY = "unity-test-key"
INSTANCE = "UnityMcp@test"
HOST = "testserver"
KID = "test-key"
MCP_HEADERS = {"accept": "application/json, text/event-stream"}
SIGNING_KEY = ec.generate_private_key(ec.SECP256R1())
ALL_TOOLS = ["unity:manage_scene", "unity:unity_docs", f"unity:{LIST_RESOURCES}"]

TOOLS = [
    {
        "name": "manage_scene",
        "inputSchema": {"type": "object", "properties": {"action": {"type": "string"}}},
    },
    {"name": "unity_docs", "inputSchema": {"type": "object", "properties": {}}},
]


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _token(
    tools: list[str] = ALL_TOOLS,
    *,
    key: ec.EllipticCurvePrivateKey = SIGNING_KEY,
    kid: str = KID,
    exp_in: int = 600,
    aud: str = TOKEN_AUDIENCE,
) -> str:
    header = _b64(pydantic_core.to_json({"alg": "ES256", "typ": "JWT", "kid": kid}))
    claims = {
        "iss": TOKEN_ISSUER,
        "aud": aud,
        "sub": "task:t",
        "iat": int(time.time()),
        "exp": int(time.time()) + exp_in,
        "task": "t",
        "lease": "l",
        "agent": "enikk",
        "session": "s",
        "tools": tools,
    }
    signing_input = f"{header}.{_b64(pydantic_core.to_json(claims))}"
    r, s = decode_dss_signature(key.sign(signing_input.encode(), ec.ECDSA(hashes.SHA256())))
    return f"{signing_input}.{_b64(r.to_bytes(32, 'big') + s.to_bytes(32, 'big'))}"


def _jwks() -> dict[str, object]:
    numbers = SIGNING_KEY.public_key().public_numbers()
    return {
        "keys": [
            {
                "kty": "EC",
                "crv": "P-256",
                "alg": "ES256",
                "use": "sig",
                "kid": KID,
                "x": _b64(numbers.x.to_bytes(32, "big")),
                "y": _b64(numbers.y.to_bytes(32, "big")),
            }
        ]
    }


class FakeCore:
    def __init__(self) -> None:
        self.fetches = 0

    def handle(self, request: httpx.Request) -> httpx.Response:
        assert str(request.url) == JWKS_URL
        self.fetches += 1
        return httpx.Response(200, json=_jwks())


class FakeUnity:
    """MCP for Unity over HTTP: event-stream replies, sessions and the key check."""

    def __init__(self, *, down: bool = False) -> None:
        self.down = down
        self.calls: list[dict[str, object]] = []
        self.sessions_opened = 0
        self.sessions_closed = 0
        self.keys: list[str | None] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        if self.down:
            raise httpx.ConnectError("no route to host")
        self.keys.append(request.headers.get(KEY_HEADER))
        if request.method == "DELETE":
            self.sessions_closed += 1
            return httpx.Response(200)
        message = pydantic_core.from_json(request.content)
        method = message["method"]
        if "id" not in message:
            return httpx.Response(202)
        if method == "initialize":
            self.sessions_opened += 1
            headers = {SESSION_HEADER: f"s{self.sessions_opened}"}
            return _event(message["id"], {"protocolVersion": "2025-06-18"}, headers)
        if request.headers.get(SESSION_HEADER) is None:
            return httpx.Response(400)
        if request.headers.get(KEY_HEADER) != KEY:
            error = {"code": 0, "message": "API key authentication required."}
            return _event(message["id"], None, error=error)
        return _event(message["id"], self._result(method, message.get("params") or {}))

    def _result(self, method: str, params: dict[str, object]) -> dict[str, object]:
        if method == "tools/list":
            return {"tools": TOOLS}
        if method == "tools/call":
            self.calls.append(params)
            return {"content": [{"type": "text", "text": "scene: Main"}], "isError": False}
        if method == "resources/list":
            return {"resources": [{"uri": "mcpforunity://editor/state", "name": "editor_state"}]}
        if method == "resources/templates/list":
            return {"resourceTemplates": [{"uriTemplate": "mcpforunity://tests/{mode}"}]}
        if method == "resources/read":
            return {"contents": [{"uri": params["uri"], "text": '{"isPlaying": false}'}]}
        raise AssertionError(method)


def _event(
    request_id: object,
    result: dict[str, object] | None,
    headers: dict[str, str] | None = None,
    *,
    error: dict[str, object] | None = None,
) -> httpx.Response:
    reply: dict[str, object] = {"jsonrpc": "2.0", "id": request_id}
    if error is not None:
        reply["error"] = error
    else:
        reply["result"] = result
    body = b"event: message\r\ndata: " + pydantic_core.to_json(reply) + b"\r\n\r\n"
    return httpx.Response(
        200, content=body, headers={"content-type": "text/event-stream", **(headers or {})}
    )


def _upstream(fake: FakeUnity, key: str = KEY) -> UnityUpstream:
    http = httpx.AsyncClient(transport=httpx.MockTransport(fake.handle))
    return UnityUpstream(http, URL, SecretStr(key), protocol_version="2025-06-18")


def _verifier(core: FakeCore | None = None) -> TokenVerifier:
    http = httpx.AsyncClient(transport=httpx.MockTransport((core or FakeCore()).handle))
    return TokenVerifier(http, JWKS_URL)


async def test_a_session_initializes_sends_the_key_and_closes() -> None:
    fake = FakeUnity()
    async with _upstream(fake).session() as session:
        result = await session.request("tools/list")
    assert [tool["name"] for tool in result["tools"]] == ["manage_scene", "unity_docs"]
    assert fake.sessions_opened == 1
    assert fake.sessions_closed == 1
    assert set(fake.keys) == {KEY}


async def test_a_json_rpc_error_is_a_request_error() -> None:
    fake = FakeUnity()
    with pytest.raises(UnityRequestError, match="API key"):
        async with _upstream(fake, key="wrong").session() as session:
            await session.request("tools/list")


async def test_an_unreachable_server_is_unavailable() -> None:
    with pytest.raises(UnityUnavailableError, match="unreachable"):
        async with _upstream(FakeUnity(down=True)).session():
            pass


async def test_tools_are_unity_s_and_the_two_resource_tools() -> None:
    relay = Relay(_upstream(FakeUnity()), instance=INSTANCE)
    result = await relay.list_tools(None)
    assert [tool.name for tool in result.tools] == [
        "manage_scene",
        "unity_docs",
        LIST_RESOURCES,
        READ_RESOURCE,
    ]


async def test_each_call_selects_the_instance_in_its_own_session() -> None:
    fake = FakeUnity()
    relay = Relay(_upstream(fake), instance=INSTANCE)
    result = await relay.call_tool("manage_scene", {"action": "get_active"})
    assert result.is_error is False
    await relay.call_tool("unity_docs", {})
    select = {"name": "set_active_instance", "arguments": {"instance": INSTANCE}}
    assert fake.calls == [
        select,
        {"name": "manage_scene", "arguments": {"action": "get_active"}},
        select,
        {"name": "unity_docs", "arguments": {}},
    ]
    assert fake.sessions_opened == 2


async def test_no_instance_is_selected_when_none_is_set() -> None:
    fake = FakeUnity()
    await Relay(_upstream(fake), instance="").call_tool("manage_scene", {"action": "get_active"})
    assert fake.calls == [{"name": "manage_scene", "arguments": {"action": "get_active"}}]


async def test_resources_are_listed_and_read_through_tools() -> None:
    relay = Relay(_upstream(FakeUnity()), instance="")
    listed = await relay.call_tool(LIST_RESOURCES, {})
    assert listed.structured_content is not None
    assert listed.structured_content["resources"][0]["name"] == "editor_state"
    assert "mcpforunity://tests/{mode}" in listed.content[0].text  # type: ignore[union-attr]
    read = await relay.call_tool(READ_RESOURCE, {"uri": "mcpforunity://editor/state"})
    assert read.content[0].text == '{"isPlaying": false}'  # type: ignore[union-attr]
    missing = await relay.call_tool(READ_RESOURCE, {})
    assert missing.is_error is True


async def test_a_call_while_unity_is_off_is_a_tool_error() -> None:
    relay = Relay(_upstream(FakeUnity(down=True)), instance=INSTANCE)
    result = await relay.call_tool("manage_scene", {"action": "get_active"})
    assert result.is_error is True
    assert "unreachable" in result.content[0].text  # type: ignore[union-attr]


async def test_a_valid_token_is_accepted_and_keys_are_cached() -> None:
    core = FakeCore()
    verifier = _verifier(core)
    claims = await verifier.verify({TOKEN_HEADER: f"Bearer {_token()}"})
    assert claims.allows("manage_scene")
    assert not claims.allows("execute_code")
    await verifier.verify({TOKEN_HEADER: _token()})
    assert core.fetches == 1


@pytest.mark.parametrize(
    ("headers", "match"),
    [
        ({}, "no task token"),
        ({TOKEN_HEADER: "Bearer "}, "no task token"),
        ({TOKEN_HEADER: "not-a-jwt"}, "malformed"),
        ({TOKEN_HEADER: _token(key=ec.generate_private_key(ec.SECP256R1()))}, "signature"),
        ({TOKEN_HEADER: _token(kid="other")}, "unknown key"),
        ({TOKEN_HEADER: _token(exp_in=-600)}, "expired"),
        ({TOKEN_HEADER: _token(aud="someone-else")}, "not core's token"),
    ],
)
async def test_bad_tokens_are_rejected(headers: dict[str, str], match: str) -> None:
    with pytest.raises(TokenRejectedError, match=match):
        await _verifier().verify(headers)


@pytest.fixture
def mcp() -> Iterator[tuple[TestClient, FakeUnity]]:
    fake = FakeUnity()
    app = create_app(Relay(_upstream(fake), instance=INSTANCE), _verifier(), allowed_hosts=(HOST,))
    with TestClient(app) as client:
        yield client, fake


def _post(client: TestClient, body: dict[str, object], token: str | None) -> dict[str, object]:
    headers = dict(MCP_HEADERS)
    if token is not None:
        headers[TOKEN_HEADER] = f"Bearer {token}"
    response = client.post(MCP_PATH, json=body, headers=headers)
    assert response.status_code == 200
    reply: dict[str, object] = response.json()
    return reply


def _call(name: str) -> dict[str, object]:
    return {
        "jsonrpc": "2.0",
        "id": 2,
        "method": "tools/call",
        "params": {"name": name, "arguments": {"action": "get_active"}},
    }


def test_health(mcp: tuple[TestClient, FakeUnity]) -> None:
    client, _ = mcp
    assert client.get(HEALTH_PATH).json() == {"status": "ok"}


def test_tools_and_calls_over_mcp_with_a_token(mcp: tuple[TestClient, FakeUnity]) -> None:
    client, fake = mcp
    listed = _post(client, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, _token())
    tools = listed["result"]["tools"]  # type: ignore[index]
    assert {tool["name"] for tool in tools} == {
        "manage_scene",
        "unity_docs",
        LIST_RESOURCES,
        READ_RESOURCE,
    }
    result = _post(client, _call("manage_scene"), _token())["result"]
    assert result["isError"] is False  # type: ignore[index]
    assert result["content"][0]["text"] == "scene: Main"  # type: ignore[index]
    assert fake.calls[-1] == {"name": "manage_scene", "arguments": {"action": "get_active"}}


def test_without_a_token_nothing_reaches_unity(mcp: tuple[TestClient, FakeUnity]) -> None:
    client, fake = mcp
    listed = _post(client, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, None)
    assert "error" in listed
    result = _post(client, _call("manage_scene"), None)["result"]
    assert result["isError"] is True  # type: ignore[index]
    assert fake.sessions_opened == 0


def test_a_tool_the_token_does_not_name_is_refused(mcp: tuple[TestClient, FakeUnity]) -> None:
    client, fake = mcp
    result = _post(client, _call("execute_code"), _token())["result"]
    assert result["isError"] is True  # type: ignore[index]
    assert "does not allow execute_code" in result["content"][0]["text"]  # type: ignore[index]
    assert fake.sessions_opened == 0
