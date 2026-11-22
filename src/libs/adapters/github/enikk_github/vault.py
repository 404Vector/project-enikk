"""Reading the owner's vault repository (ADR-0028). Writing comes with the memory tools."""

from __future__ import annotations

import base64
import binascii
from typing import TYPE_CHECKING, Final

from pydantic import BaseModel, ConfigDict, TypeAdapter, ValidationError

from enikk_core_domain.errors import DependencyUnavailableError
from enikk_kernel.models import FrozenModel

if TYPE_CHECKING:
    import httpx

    from enikk_github.app import GitHubApp, InstallationToken


class _Body(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")


class _Entry(_Body):
    path: str
    type: str
    sha: str


class _Tree(_Body):
    tree: tuple[_Entry, ...] = ()
    truncated: bool = False


class _Blob(_Body):
    content: str
    encoding: str


_TREE: Final = TypeAdapter(_Tree)
_BLOB: Final = TypeAdapter(_Blob)


class VaultFile(FrozenModel):
    path: str
    sha: str


class VaultReader:
    def __init__(self, app: GitHubApp) -> None:
        self._app = app

    def _repo(self, token: InstallationToken) -> str:
        return f"/repos/{self._app.owner}/{token.repository}"

    async def files(self, token: InstallationToken, branch: str = "main") -> list[VaultFile]:
        """Every file on ``branch`` with its blob SHA."""
        response = await self._app.call(
            "GET",
            f"{self._repo(token)}/git/trees/{branch}",
            token,
            params={"recursive": "1"},
        )
        tree = _parse(_TREE, response)
        if tree.truncated:
            raise DependencyUnavailableError("the vault tree is too large to list at once")
        return [VaultFile(path=e.path, sha=e.sha) for e in tree.tree if e.type == "blob"]

    async def text(self, token: InstallationToken, sha: str) -> str:
        """A blob as UTF-8 text; undecodable bytes are replaced."""
        blob = _parse(
            _BLOB, await self._app.call("GET", f"{self._repo(token)}/git/blobs/{sha}", token)
        )
        if blob.encoding != "base64":
            return blob.content
        try:
            return base64.b64decode(blob.content).decode("utf-8", errors="replace")
        except (binascii.Error, ValueError) as exc:
            raise DependencyUnavailableError(f"blob {sha} is not valid base64") from exc


def _parse[T](adapter: TypeAdapter[T], response: httpx.Response) -> T:
    try:
        return adapter.validate_json(response.content)
    except ValidationError:
        raise DependencyUnavailableError("GitHub returned an unexpected body") from None
