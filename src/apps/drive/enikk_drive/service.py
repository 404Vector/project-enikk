"""The exchange: put a thread's file into the exchange folder, get one out, list one
folder (ADR-0052). Every Drive path is walked from the exchange folder by name, one
folder at a time, so nothing outside it is reached. Shortcuts are never followed."""

from __future__ import annotations

import asyncio
import logging
import mimetypes
from datetime import datetime
from typing import TYPE_CHECKING, Final, Protocol

from enikk_drive.drive_api import DriveError
from enikk_drive.paths import DrivePathError, join, parse
from enikk_kernel.errors import EnikkError
from enikk_kernel.models import FrozenModel

if TYPE_CHECKING:
    import uuid
    from pathlib import Path

    from enikk_drive.drive_api import DriveFile
    from enikk_drive.files import Workspace

_log = logging.getLogger(__name__)

MY_DRIVE: Final = "root"
"""Drive's alias for the top of My Drive."""
LIST_LIMIT: Final = 200


class ExchangeError(EnikkError):
    """The request cannot be done as asked."""


class Drive(Protocol):
    async def children(
        self, folder_id: str, *, name: str | None = None, limit: int = 1000
    ) -> tuple[list[DriveFile], bool]: ...

    async def create_folder(self, parent_id: str, name: str) -> DriveFile: ...

    async def upload(
        self,
        source: Path,
        size: int,
        *,
        name: str,
        parent_id: str,
        existing_id: str | None,
        mime_type: str,
    ) -> DriveFile: ...

    async def download(self, file_id: str, target: Path, *, max_bytes: int) -> int: ...


class PutResult(FrozenModel):
    drive_path: str
    """Inside the exchange folder."""
    size: int
    replaced: bool
    modified_time: datetime | None


class GetResult(FrozenModel):
    path: str
    """The local file, under this thread's tmp."""
    size: int
    modified_time: datetime | None


class Entry(FrozenModel):
    name: str
    kind: str
    """folder, file, shortcut (never followed) or google-doc (cannot be downloaded)."""
    size: int | None
    modified_time: datetime | None


class ListResult(FrozenModel):
    folder: str
    entries: list[Entry]
    truncated: bool


class ExchangeService:
    def __init__(
        self, drive: Drive, workspace: Workspace, *, root_folder: str, max_bytes: int
    ) -> None:
        self._drive = drive
        self._workspace = workspace
        self._root_name = root_folder
        self._max_bytes = max_bytes
        self._root_id: str | None = None
        self._root_lock = asyncio.Lock()

    @property
    def root_folder(self) -> str:
        return self._root_name

    async def put(self, session: uuid.UUID, path: str, dest: str, *, overwrite: bool) -> PutResult:
        source, size = self._workspace.source(session, path, max_bytes=self._max_bytes)
        segments = self._destination(dest, source.name)
        try:
            parent = await self._folder(segments[:-1], create=True)
            name = segments[-1]
            existing = await self._one(parent, name)
            if existing is not None:
                if existing.kind != "file":
                    raise ExchangeError(f"{join(segments)} exists and is a {existing.kind}")
                if not overwrite:
                    raise ExchangeError(
                        f"{join(segments)} already exists; pass overwrite=true to replace it"
                    )
            mime = mimetypes.guess_type(name)[0] or "application/octet-stream"
            uploaded = await self._drive.upload(
                source,
                size,
                name=name,
                parent_id=parent,
                existing_id=None if existing is None else existing.id,
                mime_type=mime,
            )
        except DriveError as exc:
            self._root_id = None
            raise ExchangeError(str(exc)) from exc
        _log.info("put %s bytes=%d replaced=%s", join(segments), size, existing is not None)
        return PutResult(
            drive_path=join(segments),
            size=size,
            replaced=existing is not None,
            modified_time=uploaded.modified_time,
        )

    async def get(self, session: uuid.UUID, path: str) -> GetResult:
        segments = self._parse(path)
        try:
            parent = await self._folder(segments[:-1], create=False)
            found = await self._one(parent, segments[-1])
            if found is None:
                raise ExchangeError(f"{join(segments)}: not found")
            if found.kind != "file":
                raise ExchangeError(f"{join(segments)} is a {found.kind}; only files can be got")
            if found.size is not None and found.size > self._max_bytes:
                raise ExchangeError(
                    f"{join(segments)}: larger than {self._max_bytes // (1024 * 1024)} MiB"
                )
            target = self._workspace.target(session, segments)
            size = await self._drive.download(found.id, target, max_bytes=self._max_bytes)
        except DriveError as exc:
            self._root_id = None
            raise ExchangeError(str(exc)) from exc
        _log.info("get %s bytes=%d", join(segments), size)
        return GetResult(path=str(target), size=size, modified_time=found.modified_time)

    async def list_folder(self, folder: str) -> ListResult:
        segments = self._parse(folder, allow_root=True)
        try:
            folder_id = await self._folder(segments, create=False)
            items, truncated = await self._drive.children(folder_id, limit=LIST_LIMIT)
        except DriveError as exc:
            self._root_id = None
            raise ExchangeError(str(exc)) from exc
        return ListResult(
            folder=join(segments),
            entries=[
                Entry(
                    name=item.name,
                    kind=item.kind,
                    size=item.size,
                    modified_time=item.modified_time,
                )
                for item in items
            ],
            truncated=truncated,
        )

    @staticmethod
    def _parse(text: str, *, allow_root: bool = False) -> tuple[str, ...]:
        try:
            return parse(text, allow_root=allow_root)
        except DrivePathError as exc:
            raise ExchangeError(str(exc)) from exc

    def _destination(self, dest: str, file_name: str) -> tuple[str, ...]:
        if not dest.strip() or dest.strip().endswith("/"):
            folder = self._parse(dest, allow_root=True)
            return self._parse(join((*folder, file_name)))
        return self._parse(dest)

    async def _root(self) -> str:
        """The exchange folder at the top of My Drive, created when missing."""
        async with self._root_lock:
            if self._root_id is None:
                found = await self._folders(MY_DRIVE, self._root_name)
                if len(found) > 1:
                    raise ExchangeError(
                        f"My Drive has {len(found)} folders named {self._root_name}; "
                        "keep one or rename the others"
                    )
                if found:
                    self._root_id = found[0].id
                else:
                    created = await self._drive.create_folder(MY_DRIVE, self._root_name)
                    _log.info("created the exchange folder %s", self._root_name)
                    self._root_id = created.id
            return self._root_id

    async def _folder(self, segments: tuple[str, ...], *, create: bool) -> str:
        current = await self._root()
        for depth, name in enumerate(segments):
            found = await self._one(current, name)
            here = join(segments[: depth + 1])
            if found is None:
                if not create:
                    raise ExchangeError(f"{here}: no such folder")
                found = await self._drive.create_folder(current, name)
            elif not found.is_folder:
                raise ExchangeError(f"{here} is a {found.kind}, not a folder")
            current = found.id
        return current

    async def _folders(self, parent: str, name: str) -> list[DriveFile]:
        items, _ = await self._drive.children(parent, name=name)
        return [item for item in items if item.is_folder]

    async def _one(self, parent: str, name: str) -> DriveFile | None:
        items, _ = await self._drive.children(parent, name=name)
        if len(items) > 1:
            raise ExchangeError(
                f"{len(items)} items are named {name} in one folder; keep one or rename"
            )
        return items[0] if items else None
