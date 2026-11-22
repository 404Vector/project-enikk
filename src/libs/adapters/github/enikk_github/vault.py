"""Reading and writing the owner's vault repository (ADR-0028)."""

from __future__ import annotations

import base64
import binascii
from typing import TYPE_CHECKING, Final
from urllib.parse import quote

from pydantic import BaseModel, ConfigDict, TypeAdapter, ValidationError

from enikk_core_domain.errors import DependencyUnavailableError
from enikk_github.app import GitHubRequestError
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


class _Content(_Body):
    sha: str


class _Written(_Body):
    content: _Content


class _PutBody(FrozenModel):
    message: str
    content: str
    branch: str
    sha: str | None = None


class _DeleteBody(FrozenModel):
    message: str
    sha: str
    branch: str


_TREE: Final = TypeAdapter(_Tree)
_CONTENT: Final = TypeAdapter(_Content)
_WRITTEN: Final = TypeAdapter(_Written)
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


class VaultWriter:
    """Commits one file at a time through the contents API, on top of the branch head.

    The file's current SHA guards the write: if the owner pushed the same file in
    between, GitHub refuses it and the write is retried once on the new version.
    """

    def __init__(self, app: GitHubApp) -> None:
        self._app = app

    def _url(self, token: InstallationToken, path: str) -> str:
        return f"/repos/{self._app.owner}/{token.repository}/contents/{quote(path)}"

    async def current_sha(self, token: InstallationToken, path: str, branch: str) -> str | None:
        try:
            response = await self._app.call(
                "GET", self._url(token, path), token, params={"ref": branch}
            )
        except GitHubRequestError as exc:
            if exc.status == 404:
                return None
            raise
        return _parse(_CONTENT, response).sha

    async def put(
        self, token: InstallationToken, path: str, text: str, message: str, branch: str = "main"
    ) -> str:
        """Create or replace ``path`` with ``text``. Returns the new blob SHA."""
        encoded = base64.b64encode(text.encode("utf-8")).decode("ascii")
        for attempt in (1, 2):
            body = _PutBody(
                message=message,
                content=encoded,
                branch=branch,
                sha=await self.current_sha(token, path, branch),
            )
            try:
                response = await self._app.call(
                    "PUT",
                    self._url(token, path),
                    token,
                    body=body.model_dump_json(exclude_none=True),
                )
            except GitHubRequestError as exc:
                if exc.status in {409, 422} and attempt == 1:
                    continue
                raise
            return _parse(_WRITTEN, response).content.sha
        raise DependencyUnavailableError(f"could not write {path}")

    async def delete(
        self, token: InstallationToken, path: str, message: str, branch: str = "main"
    ) -> bool:
        """Remove ``path``. False if it was already gone."""
        for attempt in (1, 2):
            sha = await self.current_sha(token, path, branch)
            if sha is None:
                return False
            body = _DeleteBody(message=message, sha=sha, branch=branch)
            try:
                await self._app.call(
                    "DELETE", self._url(token, path), token, body=body.model_dump_json()
                )
            except GitHubRequestError as exc:
                if exc.status in {409, 422} and attempt == 1:
                    continue
                raise
            return True
        raise DependencyUnavailableError(f"could not delete {path}")


def _parse[T](adapter: TypeAdapter[T], response: httpx.Response) -> T:
    try:
        return adapter.validate_json(response.content)
    except ValidationError:
        raise DependencyUnavailableError("GitHub returned an unexpected body") from None
