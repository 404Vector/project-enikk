"""Bearer token sources for core API calls (ADR-0011 §5)."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Final, Protocol

import httpx

if TYPE_CHECKING:
    from collections.abc import Generator

    from pydantic import SecretStr

PROJECTED_TOKEN_FILE: Final = Path("/var/run/secrets/enikk/core/token")
"""Where service Pods mount their audience=enikk-core projected token."""


class TokenSource(Protocol):
    def token(self) -> str: ...


class FileTokenSource:
    """Reads the token on every call, because kubelet rotates the file."""

    def __init__(self, path: Path = PROJECTED_TOKEN_FILE) -> None:
        self._path = path

    def token(self) -> str:
        return self._path.read_text().strip()


class StaticTokenSource:
    """A fixed token, for tools that obtain one out of band (e.g. ``enikk-ops smoke``)."""

    def __init__(self, token: SecretStr) -> None:
        self._token = token

    def token(self) -> str:
        return self._token.get_secret_value()


class BearerAuth(httpx.Auth):
    def __init__(self, source: TokenSource) -> None:
        self._source = source

    def auth_flow(self, request: httpx.Request) -> Generator[httpx.Request, httpx.Response, None]:
        request.headers["Authorization"] = f"Bearer {self._source.token()}"
        yield request
