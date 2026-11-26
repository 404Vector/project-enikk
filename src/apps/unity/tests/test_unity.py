from __future__ import annotations

from typing import TYPE_CHECKING

import httpx
import pydantic_core
import pytest
from pydantic import SecretStr
from starlette.testclient import TestClient

from enikk_unity.app import HEALTH_PATH, MCP_PATH, create_app
from enikk_unity.relay import LIST_RESOURCES, READ_RESOURCE, Relay
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
KEY = "unity-test-key"
INSTANCE = "UnityMcp@test"
HOST = "testserver"
MCP_HEADERS = {"accept": "application/json, text/event-stream"}

TOOLS = [
    {
        "name": "manage_scene",
        "inputSchema": {
            "type": "object",
            "properties": {"action": {"type": "string"}, "unity_instance": {"type": "string"}},
        },
    },
    {"name": "unity_docs", "inputSchema": {"type": "object", "properties": {}}},
]


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


async def test_a_call_gets_the_instance_when_it_takes_one() -> None:
    fake = FakeUnity()
    relay = Relay(_upstream(fake), instance=INSTANCE)
    result = await relay.call_tool("manage_scene", {"action": "get_active"})
    assert result.is_error is False
    await relay.call_tool("manage_scene", {"action": "get_active", "unity_instance": "Other@1"})
    await relay.call_tool("unity_docs", {})
    assert [call["arguments"] for call in fake.calls] == [
        {"action": "get_active", "unity_instance": INSTANCE},
        {"action": "get_active", "unity_instance": "Other@1"},
        {},
    ]


async def test_no_instance_is_added_when_none_is_set() -> None:
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


@pytest.fixture
def mcp() -> Iterator[tuple[TestClient, FakeUnity]]:
    fake = FakeUnity()
    app = create_app(Relay(_upstream(fake), instance=INSTANCE), allowed_hosts=(HOST,))
    with TestClient(app) as client:
        yield client, fake


def test_health(mcp: tuple[TestClient, FakeUnity]) -> None:
    client, _ = mcp
    assert client.get(HEALTH_PATH).json() == {"status": "ok"}


def test_tools_and_calls_over_mcp(mcp: tuple[TestClient, FakeUnity]) -> None:
    client, fake = mcp
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
    tools = client.post(MCP_PATH, json=body, headers=MCP_HEADERS).json()["result"]["tools"]
    assert {tool["name"] for tool in tools} == {
        "manage_scene",
        "unity_docs",
        LIST_RESOURCES,
        READ_RESOURCE,
    }
    call = {
        "jsonrpc": "2.0",
        "id": 2,
        "method": "tools/call",
        "params": {"name": "manage_scene", "arguments": {"action": "get_active"}},
    }
    result = client.post(MCP_PATH, json=call, headers=MCP_HEADERS).json()["result"]
    assert result["isError"] is False
    assert result["content"][0]["text"] == "scene: Main"
    assert fake.calls[-1]["arguments"] == {"action": "get_active", "unity_instance": INSTANCE}
