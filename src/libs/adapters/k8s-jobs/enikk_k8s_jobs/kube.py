"""What the Kubernetes API readers share: the ServiceAccount files and bearer auth."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Final

import httpx

if TYPE_CHECKING:
    from collections.abc import Generator

SERVICE_ACCOUNT_DIR: Final = Path("/var/run/secrets/kubernetes.io/serviceaccount")


class FileBearerAuth(httpx.Auth):
    """Sends the token stored in ``path``, re-read on every request (kubelet rotates it)."""

    def __init__(self, path: Path) -> None:
        self._path = path

    def auth_flow(self, request: httpx.Request) -> Generator[httpx.Request, httpx.Response, None]:
        request.headers["Authorization"] = f"Bearer {self._path.read_text().strip()}"
        yield request
