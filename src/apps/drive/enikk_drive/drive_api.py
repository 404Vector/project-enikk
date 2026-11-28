"""Google OAuth and the Drive v3 REST calls the exchange tools need (ADR-0053 §2).

The refresh token becomes a short access token, cached until a minute before it
expires. Uploads use resumable sessions in chunks; downloads stream to a file.
"""

from __future__ import annotations

import asyncio
import os
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Final

import httpx
import pydantic_core
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, ValidationError

from enikk_kernel.errors import EnikkError
from enikk_kernel.models import FrozenModel

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from pydantic import SecretStr

FOLDER_MIME: Final = "application/vnd.google-apps.folder"
SHORTCUT_MIME: Final = "application/vnd.google-apps.shortcut"
GOOGLE_APPS_PREFIX: Final = "application/vnd.google-apps."
CHUNK_BYTES: Final = 8 * 1024 * 1024
"""Upload chunk size; a multiple of 256 KiB as resumable uploads require."""
FILE_FIELDS: Final = "id,name,mimeType,size,modifiedTime"
TOKEN_MARGIN: Final = timedelta(seconds=60)
PAGE_SIZE: Final = 100


class DriveError(EnikkError):
    """Google refused or failed a request. Never holds a token."""


class DriveFile(FrozenModel):
    id: str
    name: str
    mime_type: str
    size: int | None
    modified_time: datetime | None

    @property
    def is_folder(self) -> bool:
        return self.mime_type == FOLDER_MIME

    @property
    def kind(self) -> str:
        if self.mime_type == FOLDER_MIME:
            return "folder"
        if self.mime_type == SHORTCUT_MIME:
            return "shortcut"
        if self.mime_type.startswith(GOOGLE_APPS_PREFIX):
            return "google-doc"
        return "file"


class _Wire(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="ignore")


class _File(_Wire):
    id: str
    name: str
    mime_type: str = Field(alias="mimeType")
    size: int | None = None
    modified_time: AwareDatetime | None = Field(default=None, alias="modifiedTime")

    def model(self) -> DriveFile:
        return DriveFile(
            id=self.id,
            name=self.name,
            mime_type=self.mime_type,
            size=self.size,
            modified_time=self.modified_time,
        )


class _FileList(_Wire):
    files: list[_File] = []
    next_page_token: str | None = Field(default=None, alias="nextPageToken")


class _Token(_Wire):
    access_token: str
    expires_in: int = 3600


class _ErrorBody(_Wire):
    message: str = ""
    status: str = ""


class _Error(_Wire):
    error: _ErrorBody | str | None = None
    error_description: str = ""


def quote(value: str) -> str:
    """A string literal for a Drive search query."""
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


def _failure(what: str, response: httpx.Response) -> DriveError:
    detail = ""
    try:
        parsed = _Error.model_validate_json(response.content)
    except ValidationError:
        parsed = None
    if parsed is not None:
        if isinstance(parsed.error, _ErrorBody):
            detail = " ".join(p for p in (parsed.error.status, parsed.error.message) if p)
        elif isinstance(parsed.error, str):
            detail = " ".join(p for p in (parsed.error, parsed.error_description) if p)
    return DriveError(f"{what}: {response.status_code} {detail}".rstrip())


class OAuthToken:
    """Access tokens from the owner's refresh token (installed-app OAuth client)."""

    def __init__(
        self,
        http: httpx.AsyncClient,
        token_url: str,
        *,
        client_id: SecretStr,
        client_secret: SecretStr,
        refresh_token: SecretStr,
        now: Callable[[], datetime],
    ) -> None:
        self._http = http
        self._url = token_url
        self._client_id = client_id
        self._client_secret = client_secret
        self._refresh_token = refresh_token
        self._now = now
        self._lock = asyncio.Lock()
        self._token: str | None = None
        self._expires: datetime | None = None

    def forget(self) -> None:
        self._token = None

    async def bearer(self) -> str:
        async with self._lock:
            if self._token is None or self._expires is None or self._now() >= self._expires:
                await self._refresh()
            if self._token is None:
                raise DriveError("token refresh gave no access token")
            return self._token

    async def _refresh(self) -> None:
        form = {
            "grant_type": "refresh_token",
            "client_id": self._client_id.get_secret_value(),
            "client_secret": self._client_secret.get_secret_value(),
            "refresh_token": self._refresh_token.get_secret_value(),
        }
        try:
            response = await self._http.post(self._url, data=form)
        except httpx.HTTPError as exc:
            raise DriveError(f"token refresh failed: {type(exc).__name__}") from exc
        if response.status_code != 200:
            raise _failure("token refresh", response)
        try:
            token = _Token.model_validate_json(response.content)
        except ValidationError as exc:
            raise DriveError("token refresh: unexpected response") from exc
        self._token = token.access_token
        self._expires = self._now() + timedelta(seconds=token.expires_in) - TOKEN_MARGIN


class DriveApi:
    def __init__(
        self, http: httpx.AsyncClient, token: OAuthToken, *, api_url: str, upload_url: str
    ) -> None:
        self._http = http
        self._token = token
        self._api = api_url.rstrip("/")
        self._upload = upload_url.rstrip("/")

    async def _headers(self) -> dict[str, str]:
        return {"authorization": f"Bearer {await self._token.bearer()}"}

    async def _json(
        self,
        what: str,
        method: str,
        url: str,
        *,
        params: dict[str, str] | None = None,
        body: object = None,
    ) -> bytes:
        """One JSON request; a 401 refreshes the token and tries once more."""
        for attempt in range(2):
            content = None if body is None else pydantic_core.to_json(body)
            headers = await self._headers()
            if content is not None:
                headers["content-type"] = "application/json; charset=UTF-8"
            try:
                response = await self._http.request(
                    method, url, params=params, content=content, headers=headers
                )
            except httpx.HTTPError as exc:
                raise DriveError(f"{what} failed: {type(exc).__name__}") from exc
            if response.status_code == 401 and attempt == 0:
                self._token.forget()
                continue
            if response.status_code >= 300:
                raise _failure(what, response)
            return response.content
        raise DriveError(f"{what}: unauthorized")

    async def children(
        self, folder_id: str, *, name: str | None = None, limit: int = 1000
    ) -> tuple[list[DriveFile], bool]:
        """Items directly in a folder, not trashed; with ``name``, only that name. And
        whether more were left out."""
        query = f"{quote(folder_id)} in parents and trashed = false"
        if name is not None:
            query += f" and name = {quote(name)}"
        found: list[DriveFile] = []
        page: str | None = None
        while True:
            params = {
                "q": query,
                "fields": f"nextPageToken,files({FILE_FIELDS})",
                "pageSize": str(min(PAGE_SIZE, limit)),
                "orderBy": "folder,name",
                "spaces": "drive",
            }
            if page is not None:
                params["pageToken"] = page
            raw = await self._json("list", "GET", f"{self._api}/files", params=params)
            listed = _FileList.model_validate_json(raw)
            found += [item.model() for item in listed.files]
            page = listed.next_page_token
            if page is None:
                return found[:limit], len(found) > limit
            if len(found) >= limit:
                return found[:limit], True

    async def create_folder(self, parent_id: str, name: str) -> DriveFile:
        body = {"name": name, "mimeType": FOLDER_MIME, "parents": [parent_id]}
        raw = await self._json(
            "create folder", "POST", f"{self._api}/files", params={"fields": FILE_FIELDS}, body=body
        )
        return _File.model_validate_json(raw).model()

    async def upload(
        self,
        source: Path,
        size: int,
        *,
        name: str,
        parent_id: str,
        existing_id: str | None,
        mime_type: str,
    ) -> DriveFile:
        """A new file in ``parent_id``, or a new revision of ``existing_id``."""
        session = await self._start_upload(size, name, parent_id, existing_id, mime_type)
        headers = await self._headers()
        try:
            if size == 0:
                response = await self._http.put(
                    session, content=b"", headers={**headers, "content-range": "bytes */0"}
                )
                return self._uploaded(response)
            with source.open("rb") as handle:
                start = 0
                while start < size:
                    chunk = handle.read(CHUNK_BYTES)
                    if not chunk:
                        raise DriveError(f"{source.name} changed while uploading")
                    end = start + len(chunk) - 1
                    response = await self._http.put(
                        session,
                        content=chunk,
                        headers={**headers, "content-range": f"bytes {start}-{end}/{size}"},
                    )
                    if response.status_code == 308:
                        start = end + 1
                        continue
                    return self._uploaded(response)
        except OSError as exc:
            raise DriveError(f"cannot read {source.name} ({exc.strerror})") from exc
        except httpx.HTTPError as exc:
            raise DriveError(f"upload failed: {type(exc).__name__}") from exc
        raise DriveError("upload ended without a result")

    async def _start_upload(
        self, size: int, name: str, parent_id: str, existing_id: str | None, mime_type: str
    ) -> str:
        headers = {
            **(await self._headers()),
            "content-type": "application/json; charset=UTF-8",
            "x-upload-content-type": mime_type,
            "x-upload-content-length": str(size),
        }
        params = {"uploadType": "resumable", "fields": FILE_FIELDS}
        if existing_id is None:
            method, url = "POST", f"{self._upload}/files"
            body: dict[str, object] = {"name": name, "parents": [parent_id]}
        else:
            method, url = "PATCH", f"{self._upload}/files/{existing_id}"
            body = {}
        try:
            response = await self._http.request(
                method, url, params=params, content=pydantic_core.to_json(body), headers=headers
            )
        except httpx.HTTPError as exc:
            raise DriveError(f"upload failed: {type(exc).__name__}") from exc
        if response.status_code >= 300:
            raise _failure("start upload", response)
        location = str(response.headers.get("location") or "")
        if not location:
            raise DriveError("start upload: no upload session in the response")
        return location

    @staticmethod
    def _uploaded(response: httpx.Response) -> DriveFile:
        if response.status_code not in {200, 201}:
            raise _failure("upload", response)
        return _File.model_validate_json(response.content).model()

    async def download(self, file_id: str, target: Path, *, max_bytes: int) -> int:
        """The file's bytes into ``target``, through a temporary file beside it."""
        partial = target.with_name(f".{target.name}.part")
        written = 0
        try:
            async with self._http.stream(
                "GET",
                f"{self._api}/files/{file_id}",
                params={"alt": "media"},
                headers=await self._headers(),
            ) as response:
                if response.status_code >= 300:
                    await response.aread()
                    raise _failure("download", response)
                with partial.open("wb") as handle:
                    async for chunk in response.aiter_bytes():
                        written += len(chunk)
                        if written > max_bytes:
                            raise DriveError(
                                f"download: larger than {max_bytes // (1024 * 1024)} MiB"
                            )
                        handle.write(chunk)
            os.replace(partial, target)
        except httpx.HTTPError as exc:
            raise DriveError(f"download failed: {type(exc).__name__}") from exc
        except OSError as exc:
            raise DriveError(f"cannot write {target.name} ({exc.strerror})") from exc
        finally:
            partial.unlink(missing_ok=True)
        return written
