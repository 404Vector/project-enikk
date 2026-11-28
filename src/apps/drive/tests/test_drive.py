from __future__ import annotations

import itertools
import uuid
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import httpx
import pydantic_core
import pytest
from pydantic import SecretStr
from starlette.testclient import TestClient

from enikk_drive import drive_api
from enikk_drive.app import HEALTH_PATH, MCP_PATH, create_app
from enikk_drive.drive_api import (
    FOLDER_MIME,
    SHORTCUT_MIME,
    DriveApi,
    DriveError,
    DriveFile,
    OAuthToken,
    quote,
)
from enikk_drive.files import LocalFileError, Workspace
from enikk_drive.paths import DrivePathError, parse
from enikk_drive.service import MY_DRIVE, ExchangeError, ExchangeService
from enikk_drive.tools import SESSION_HEADER

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

pytestmark = pytest.mark.anyio

SESSION = uuid.uuid4()
OTHER = uuid.uuid4()
HOST = "testserver"
MCP_HEADERS = {"accept": "application/json, text/event-stream", SESSION_HEADER: str(SESSION)}
MB = 1024 * 1024
WHEN = datetime(2026, 11, 28, 5, 0, tzinfo=UTC)


class FakeDrive:
    """My Drive in memory: items by id with their parent and bytes."""

    def __init__(self) -> None:
        self.items: dict[str, tuple[DriveFile, str, bytes]] = {}
        self._ids = itertools.count(1)
        self.uploads: list[tuple[str, str | None, str]] = []

    def add(self, parent: str, name: str, mime: str = "image/png", data: bytes = b"") -> str:
        file_id = f"id{next(self._ids)}"
        size = None if mime.startswith("application/vnd.google-apps.") else len(data)
        item = DriveFile(id=file_id, name=name, mime_type=mime, size=size, modified_time=WHEN)
        self.items[file_id] = (item, parent, data)
        return file_id

    def path_of(self, file_id: str) -> str:
        names: list[str] = []
        current = file_id
        while current != MY_DRIVE:
            item, parent, _ = self.items[current]
            names.append(item.name)
            current = parent
        return "/".join(reversed(names))

    async def children(
        self, folder_id: str, *, name: str | None = None, limit: int = 1000
    ) -> tuple[list[DriveFile], bool]:
        found = [
            item
            for item, parent, _ in self.items.values()
            if parent == folder_id and (name is None or item.name == name)
        ]
        return found[:limit], len(found) > limit

    async def create_folder(self, parent_id: str, name: str) -> DriveFile:
        return self.items[self.add(parent_id, name, FOLDER_MIME)][0]

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
        data = source.read_bytes()
        assert len(data) == size
        self.uploads.append((name, existing_id, mime_type))
        if existing_id is None:
            return self.items[self.add(parent_id, name, mime_type, data)][0]
        item, parent, _ = self.items[existing_id]
        self.items[existing_id] = (item, parent, data)
        return item

    async def download(self, file_id: str, target: Path, *, max_bytes: int) -> int:
        _, _, data = self.items[file_id]
        target.write_bytes(data)
        return len(data)


def _own(tmp_path: Path, name: str = "hero.png", data: bytes = b"png-bytes") -> Path:
    path = tmp_path / "tmp" / str(SESSION) / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def _service(tmp_path: Path, drive: FakeDrive, max_bytes: int = MB) -> ExchangeService:
    return ExchangeService(drive, Workspace(tmp_path), root_folder="enikk", max_bytes=max_bytes)


# ---------------------------------------------------------------- paths


def test_paths_are_split_and_checked() -> None:
    assert parse("to-unity/hero.png") == ("to-unity", "hero.png")
    assert parse("/to-unity/hero.png/") == ("to-unity", "hero.png")
    assert parse("캐릭터/리타 v2.png") == ("캐릭터", "리타 v2.png")
    assert parse("", allow_root=True) == ()
    for bad in ["", "a//b", "../x", "a/./b", "a\\b", "a/\x00b", "a /b", "/".join("a" * 9)]:
        with pytest.raises(DrivePathError):
            parse(bad)


def test_query_literals_are_escaped() -> None:
    assert quote("it's") == "'it\\'s'"
    assert quote("a\\b") == "'a\\\\b'"


# ---------------------------------------------------------------- local files


def test_only_the_threads_own_files_can_be_put(tmp_path: Path) -> None:
    workspace = Workspace(tmp_path)
    own = _own(tmp_path)
    assert workspace.source(SESSION, str(own), max_bytes=MB) == (own.resolve(), 9)
    assert workspace.source(SESSION, "hero.png", max_bytes=MB)[0] == own.resolve()
    other = tmp_path / "tmp" / str(OTHER) / "x.png"
    other.parent.mkdir(parents=True)
    other.write_bytes(b"x")
    with pytest.raises(LocalFileError, match="only files under this thread"):
        workspace.source(SESSION, str(other), max_bytes=MB)
    with pytest.raises(LocalFileError, match="only files under this thread"):
        workspace.source(SESSION, f"../{OTHER}/x.png", max_bytes=MB)
    with pytest.raises(LocalFileError, match="larger than"):
        workspace.source(SESSION, str(own), max_bytes=4)
    with pytest.raises(LocalFileError, match="not a file"):
        workspace.source(SESSION, str(own.parent), max_bytes=MB)


# ---------------------------------------------------------------- service


async def test_put_creates_the_exchange_folder_and_subfolders(tmp_path: Path) -> None:
    drive = FakeDrive()
    service = _service(tmp_path, drive)
    _own(tmp_path)
    result = await service.put(SESSION, "hero.png", "to-unity/", overwrite=False)
    assert result.drive_path == "to-unity/hero.png"
    assert result.replaced is False
    paths = sorted(drive.path_of(i) for i in drive.items)
    assert paths == ["enikk", "enikk/to-unity", "enikk/to-unity/hero.png"]
    assert drive.uploads == [("hero.png", None, "image/png")]


async def test_put_with_an_empty_dest_goes_to_the_top(tmp_path: Path) -> None:
    drive = FakeDrive()
    _own(tmp_path, "notes.txt", b"hi")
    result = await _service(tmp_path, drive).put(SESSION, "notes.txt", "", overwrite=False)
    assert result.drive_path == "notes.txt"


async def test_an_existing_file_is_replaced_only_when_asked(tmp_path: Path) -> None:
    drive = FakeDrive()
    service = _service(tmp_path, drive)
    _own(tmp_path)
    await service.put(SESSION, "hero.png", "a/hero.png", overwrite=False)
    with pytest.raises(ExchangeError, match="overwrite=true"):
        await service.put(SESSION, "hero.png", "a/hero.png", overwrite=False)
    _own(tmp_path, data=b"new")
    result = await service.put(SESSION, "hero.png", "a/hero.png", overwrite=True)
    assert result.replaced is True
    assert len([i for i in drive.items.values() if i[0].name == "hero.png"]) == 1
    assert drive.uploads[-1][1] is not None


async def test_get_downloads_into_the_threads_drive_folder(tmp_path: Path) -> None:
    drive = FakeDrive()
    root = drive.add(MY_DRIVE, "enikk", FOLDER_MIME)
    folder = drive.add(root, "from-unity", FOLDER_MIME)
    drive.add(folder, "shot.png", data=b"shot")
    result = await _service(tmp_path, drive).get(SESSION, "from-unity/shot.png")
    expected = tmp_path / "tmp" / str(SESSION) / "drive" / "from-unity" / "shot.png"
    assert result.path == str(expected)
    assert expected.read_bytes() == b"shot"
    assert result.size == 4


async def test_get_refuses_what_is_not_a_plain_file(tmp_path: Path) -> None:
    drive = FakeDrive()
    root = drive.add(MY_DRIVE, "enikk", FOLDER_MIME)
    drive.add(root, "doc", "application/vnd.google-apps.document")
    drive.add(root, "link", SHORTCUT_MIME)
    drive.add(root, "sub", FOLDER_MIME)
    drive.add(root, "big.bin", "application/octet-stream", b"x" * 10)
    service = _service(tmp_path, drive, max_bytes=5)
    with pytest.raises(ExchangeError, match="google-doc"):
        await service.get(SESSION, "doc")
    with pytest.raises(ExchangeError, match="shortcut"):
        await service.get(SESSION, "link")
    with pytest.raises(ExchangeError, match="folder"):
        await service.get(SESSION, "sub")
    with pytest.raises(ExchangeError, match="larger than"):
        await service.get(SESSION, "big.bin")
    with pytest.raises(ExchangeError, match="not found"):
        await service.get(SESSION, "missing.png")


async def test_nothing_outside_the_exchange_folder_is_reached(tmp_path: Path) -> None:
    drive = FakeDrive()
    root = drive.add(MY_DRIVE, "enikk", FOLDER_MIME)
    private = drive.add(MY_DRIVE, "Private", FOLDER_MIME)
    drive.add(private, "secret.pdf", "application/pdf", b"pdf")
    drive.add(MY_DRIVE, "top.png", data=b"top")
    drive.add(root, "Private", SHORTCUT_MIME)
    service = _service(tmp_path, drive)
    with pytest.raises(ExchangeError, match="shortcut, not a folder"):
        await service.get(SESSION, "Private/secret.pdf")
    with pytest.raises(ExchangeError, match="not found"):
        await service.get(SESSION, "top.png")
    with pytest.raises(ExchangeError):
        await service.get(SESSION, "../top.png")
    listed = await service.list_folder("")
    assert [entry.name for entry in listed.entries] == ["Private"]
    assert listed.entries[0].kind == "shortcut"


async def test_ambiguous_names_are_refused(tmp_path: Path) -> None:
    drive = FakeDrive()
    drive.add(MY_DRIVE, "enikk", FOLDER_MIME)
    drive.add(MY_DRIVE, "enikk", FOLDER_MIME)
    with pytest.raises(ExchangeError, match="2 folders named enikk"):
        await _service(tmp_path, drive).list_folder("")
    drive = FakeDrive()
    root = drive.add(MY_DRIVE, "enikk", FOLDER_MIME)
    drive.add(root, "a.png", data=b"1")
    drive.add(root, "a.png", data=b"2")
    with pytest.raises(ExchangeError, match=r"2 items are named a\.png"):
        await _service(tmp_path, drive).get(SESSION, "a.png")


async def test_list_shows_one_folder(tmp_path: Path) -> None:
    drive = FakeDrive()
    root = drive.add(MY_DRIVE, "enikk", FOLDER_MIME)
    sub = drive.add(root, "to-unity", FOLDER_MIME)
    drive.add(sub, "hero.png", data=b"abc")
    service = _service(tmp_path, drive)
    listed = await service.list_folder("to-unity")
    assert listed.folder == "to-unity"
    assert [(e.name, e.kind, e.size) for e in listed.entries] == [("hero.png", "file", 3)]
    with pytest.raises(ExchangeError, match="no such folder"):
        await service.list_folder("nope")


# ---------------------------------------------------------------- Drive REST client


class Clock:
    def __init__(self) -> None:
        self.now = WHEN

    def __call__(self) -> datetime:
        return self.now


def _json(body: object, status: int = 200, headers: dict[str, str] | None = None) -> httpx.Response:
    return httpx.Response(status, content=pydantic_core.to_json(body), headers=headers)


def _api(handler: httpx.MockTransport, clock: Clock | None = None) -> tuple[DriveApi, OAuthToken]:
    http = httpx.AsyncClient(transport=handler)
    token = OAuthToken(
        http,
        "https://oauth.test/token",
        client_id=SecretStr("cid"),
        client_secret=SecretStr("csecret"),
        refresh_token=SecretStr("rtoken"),
        now=clock or Clock(),
    )
    api = DriveApi(
        http, token, api_url="https://drive.test/v3", upload_url="https://upload.test/v3"
    )
    return api, token


def _token_or(seen: list[httpx.Request], answer: httpx.Response) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.host == "oauth.test":
            return _json({"access_token": f"at{len(seen)}", "expires_in": 3600})
        return answer

    return httpx.MockTransport(handler)


async def test_token_is_refreshed_once_and_sent_as_a_bearer() -> None:
    seen: list[httpx.Request] = []
    clock = Clock()
    api, _ = _api(_token_or(seen, _json({"files": []})), clock)
    await api.children("folder-1", name="it's")
    await api.children("folder-1")
    hosts = [r.url.host for r in seen]
    assert hosts == ["oauth.test", "drive.test", "drive.test"]
    form = seen[0].content.decode()
    assert "grant_type=refresh_token" in form
    assert "refresh_token=rtoken" in form
    assert seen[1].headers["authorization"] == "Bearer at1"
    assert (
        seen[1].url.params["q"] == "'folder-1' in parents and trashed = false and name = 'it\\'s'"
    )
    assert "rtoken" not in str(seen[1].url)
    clock.now += timedelta(hours=1)
    await api.children("folder-1")
    assert [r.url.host for r in seen][-2:] == ["oauth.test", "drive.test"]


async def test_a_401_refreshes_the_token_and_retries_once() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.host == "oauth.test":
            return _json({"access_token": f"at{len(seen)}", "expires_in": 3600})
        if request.headers["authorization"] == "Bearer at1":
            return _json({"error": {"code": 401, "message": "expired"}}, 401)
        return _json({"files": [{"id": "f", "name": "a", "mimeType": FOLDER_MIME}]})

    api, _ = _api(httpx.MockTransport(handler))
    items, more = await api.children("x")
    assert [item.name for item in items] == ["a"]
    assert more is False
    assert [r.url.host for r in seen] == ["oauth.test", "drive.test", "oauth.test", "drive.test"]


async def test_errors_carry_the_status_but_no_secret() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "oauth.test":
            return _json({"error": "invalid_grant", "error_description": "Token revoked."}, 400)
        return _json({})

    api, _ = _api(httpx.MockTransport(handler))
    with pytest.raises(DriveError) as caught:
        await api.children("x")
    message = str(caught.value)
    assert "400 invalid_grant Token revoked." in message
    assert "rtoken" not in message
    assert "csecret" not in message


async def test_resumable_upload_sends_chunks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(drive_api, "CHUNK_BYTES", 4)
    source = tmp_path / "a.bin"
    source.write_bytes(b"0123456789")
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.host == "oauth.test":
            return _json({"access_token": "at", "expires_in": 3600})
        if request.url.params.get("uploadType") == "resumable":
            return httpx.Response(200, headers={"location": "https://upload.test/session/1"})
        if request.headers["content-range"].endswith("9/10"):
            body = {"id": "new", "name": "a.bin", "mimeType": "application/octet-stream"}
            return _json({**body, "size": "10", "modifiedTime": "2026-11-28T05:00:00Z"})
        return httpx.Response(308)

    api, _ = _api(httpx.MockTransport(handler))
    made = await api.upload(
        source, 10, name="a.bin", parent_id="p", existing_id=None, mime_type="x/y"
    )
    assert made.id == "new"
    assert made.size == 10
    assert made.modified_time == WHEN
    start = seen[1]
    assert start.method == "POST"
    assert start.url.path == "/v3/files"
    assert start.headers["x-upload-content-length"] == "10"
    assert pydantic_core.from_json(start.content) == {"name": "a.bin", "parents": ["p"]}
    ranges = [r.headers["content-range"] for r in seen[2:]]
    assert ranges == ["bytes 0-3/10", "bytes 4-7/10", "bytes 8-9/10"]
    assert b"".join(r.content for r in seen[2:]) == b"0123456789"


async def test_a_new_revision_patches_the_existing_file(tmp_path: Path) -> None:
    source = tmp_path / "a.bin"
    source.write_bytes(b"")
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.host == "oauth.test":
            return _json({"access_token": "at", "expires_in": 3600})
        if request.url.params.get("uploadType") == "resumable":
            return httpx.Response(200, headers={"location": "https://upload.test/session/2"})
        return _json({"id": "old", "name": "a.bin", "mimeType": "x/y", "size": "0"})

    api, _ = _api(httpx.MockTransport(handler))
    await api.upload(source, 0, name="a.bin", parent_id="p", existing_id="old", mime_type="x/y")
    assert seen[1].method == "PATCH"
    assert seen[1].url.path == "/v3/files/old"
    assert seen[2].headers["content-range"] == "bytes */0"


async def test_download_streams_to_the_file_and_stops_at_the_limit(tmp_path: Path) -> None:
    seen: list[httpx.Request] = []
    api, _ = _api(_token_or(seen, httpx.Response(200, content=b"abcdef")))
    target = tmp_path / "out.bin"
    assert await api.download("f1", target, max_bytes=10) == 6
    assert target.read_bytes() == b"abcdef"
    assert seen[1].url.params["alt"] == "media"
    with pytest.raises(DriveError, match="larger than"):
        await api.download("f1", tmp_path / "small.bin", max_bytes=3)
    assert not (tmp_path / "small.bin").exists()
    assert not (tmp_path / ".small.bin.part").exists()


# ---------------------------------------------------------------- MCP


@pytest.fixture
def mcp(tmp_path: Path) -> Iterator[tuple[TestClient, FakeDrive]]:
    drive = FakeDrive()
    app = create_app(_service(tmp_path, drive), allowed_hosts=(HOST,))
    with TestClient(app) as client:
        yield client, drive


def _call(
    client: TestClient, name: str, arguments: dict[str, object], headers: dict[str, str]
) -> dict[str, object]:
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": name, "arguments": arguments},
    }
    response = client.post(MCP_PATH, json=body, headers=headers)
    assert response.status_code == 200
    result: dict[str, object] = response.json()["result"]
    return result


def test_health(mcp: tuple[TestClient, FakeDrive]) -> None:
    client, _ = mcp
    assert client.get(HEALTH_PATH).json() == {"status": "ok"}


def test_tools_are_listed(mcp: tuple[TestClient, FakeDrive]) -> None:
    client, _ = mcp
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
    tools = client.post(MCP_PATH, json=body, headers=MCP_HEADERS).json()["result"]["tools"]
    assert {tool["name"] for tool in tools} == {"drive_put", "drive_get", "drive_list"}


def test_put_and_get_over_mcp(mcp: tuple[TestClient, FakeDrive], tmp_path: Path) -> None:
    client, _ = mcp
    _own(tmp_path)
    put = _call(client, "drive_put", {"path": "hero.png", "dest": "to-unity/"}, MCP_HEADERS)
    assert put["isError"] is False
    assert put["structuredContent"]["drive_path"] == "to-unity/hero.png"  # type: ignore[index]
    got = _call(client, "drive_get", {"path": "to-unity/hero.png"}, MCP_HEADERS)
    assert got["isError"] is False
    path = str(got["structuredContent"]["path"])  # type: ignore[index]
    assert path.startswith(str(tmp_path / "tmp" / str(SESSION) / "drive"))


def test_a_call_without_a_session_is_refused(mcp: tuple[TestClient, FakeDrive]) -> None:
    client, drive = mcp
    headers = {"accept": MCP_HEADERS["accept"]}
    result = _call(client, "drive_put", {"path": "x.png"}, headers)
    assert result["isError"] is True
    assert drive.items == {}
