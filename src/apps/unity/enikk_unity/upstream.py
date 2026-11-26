"""A short MCP session with the Unity server: initialize, the requests, close.

Each relayed request gets its own session, so nothing depends on the server's session
state or its idle timeout (ADR-0044 §2). The key goes in ``X-API-Key`` on every request.
"""

from __future__ import annotations

import contextlib
import itertools
import logging
from typing import TYPE_CHECKING, Any, Final

import httpx
import pydantic_core

from enikk_kernel.errors import EnikkError

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from pydantic import SecretStr

KEY_HEADER: Final = "X-API-Key"
SESSION_HEADER: Final = "Mcp-Session-Id"
VERSION_HEADER: Final = "MCP-Protocol-Version"
CLIENT_NAME: Final = "enikk-unity"

_log = logging.getLogger(__name__)


class UnityUnavailableError(EnikkError):
    """The Unity server could not be reached or answered with something unusable."""


class UnityRequestError(EnikkError):
    """The Unity server answered a request with a JSON-RPC error."""


class UnityUpstream:
    """Opens sessions with the Unity MCP server."""

    def __init__(
        self, http: httpx.AsyncClient, url: str, key: SecretStr, *, protocol_version: str
    ) -> None:
        self._http = http
        self._url = url
        self._key = key
        self._version = protocol_version

    @contextlib.asynccontextmanager
    async def session(self) -> AsyncIterator[UnitySession]:
        session = UnitySession(self._http, self._url, self._key, self._version)
        await session.open()
        try:
            yield session
        finally:
            await session.close()


class UnitySession:
    def __init__(self, http: httpx.AsyncClient, url: str, key: SecretStr, version: str) -> None:
        self._http = http
        self._url = url
        self._key = key
        self._version = version
        self._session_id: str | None = None
        self._ids = itertools.count(1)

    async def open(self) -> None:
        params = {
            "protocolVersion": self._version,
            "capabilities": {},
            "clientInfo": {"name": CLIENT_NAME, "version": "0"},
        }
        await self.request("initialize", params)
        await self._post({"jsonrpc": "2.0", "method": "notifications/initialized"})

    async def request(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """The ``result`` of one JSON-RPC request."""
        request_id = next(self._ids)
        message: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id, "method": method}
        if params is not None:
            message["params"] = params
        response = await self._post(message)
        if method == "initialize":
            self._session_id = response.headers.get(SESSION_HEADER)
        reply = _reply(response, request_id)
        error = reply.get("error")
        if error is not None:
            text = error.get("message", "") if isinstance(error, dict) else str(error)
            raise UnityRequestError(f"Unity MCP {method}: {text}")
        result = reply.get("result")
        if not isinstance(result, dict):
            raise UnityUnavailableError(f"Unity MCP {method}: no result in the reply")
        return result

    async def close(self) -> None:
        if self._session_id is None:
            return
        try:
            await self._http.delete(self._url, headers=self._headers())
        except httpx.HTTPError as exc:
            _log.debug("closing the Unity session failed: %s", exc)

    async def _post(self, message: dict[str, Any]) -> httpx.Response:
        try:
            response = await self._http.post(
                self._url, content=pydantic_core.to_json(message), headers=self._headers()
            )
        except httpx.HTTPError as exc:
            raise UnityUnavailableError(f"Unity MCP server unreachable: {exc}") from exc
        if response.status_code >= 400:
            raise UnityUnavailableError(f"Unity MCP server answered HTTP {response.status_code}")
        return response

    def _headers(self) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            VERSION_HEADER: self._version,
            KEY_HEADER: self._key.get_secret_value(),
        }
        if self._session_id is not None:
            headers[SESSION_HEADER] = self._session_id
        return headers


def _reply(response: httpx.Response, request_id: int) -> dict[str, Any]:
    """The JSON-RPC response to ``request_id`` in a JSON or event-stream body."""
    content_type = response.headers.get("content-type", "")
    if content_type.startswith("text/event-stream"):
        messages = [_parse(data) for data in _events(response.text)]
    else:
        messages = [_parse(response.text)]
    for message in messages:
        if isinstance(message, dict) and message.get("id") == request_id:
            return message
    raise UnityUnavailableError("Unity MCP server sent no reply to the request")


def _events(body: str) -> list[str]:
    """The data of each server-sent event."""
    events: list[str] = []
    lines: list[str] = []
    for line in [*body.splitlines(), ""]:
        if line.startswith("data:"):
            lines.append(line[5:].removeprefix(" "))
        elif not line and lines:
            events.append("\n".join(lines))
            lines = []
    return events


def _parse(text: str) -> object:
    try:
        return pydantic_core.from_json(text)
    except ValueError as exc:
        raise UnityUnavailableError("Unity MCP server sent a body that is not JSON") from exc
