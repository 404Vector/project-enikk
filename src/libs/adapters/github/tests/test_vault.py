from __future__ import annotations

import base64
import json
from datetime import UTC, datetime

import httpx
import pytest
from pydantic import SecretStr

from enikk_github.app import GitHubApp, GitHubAppConfig, InstallationToken
from enikk_github.vault import VaultReader, VaultWriter
from enikk_kernel.testing import ManualClock

pytestmark = pytest.mark.anyio

CONFIG = GitHubAppConfig(app_id=5869950, installation_id=184545127, owner="404Vector")
TOKEN = InstallationToken(
    token=SecretStr("ghs_memory"),
    expires_at=datetime(2026, 11, 22, 9, tzinfo=UTC),
    repository="vault.hyeongseok-kim",
    permissions={"contents": "write"},
)
PATH = "vault/Private/enikk/선호/배포 알림.md"


def _app(handler: httpx.MockTransport) -> GitHubApp:
    return GitHubApp.create(CONFIG, lambda: None, ManualClock(), transport=handler)


async def test_put_creates_then_replaces_with_the_current_sha() -> None:
    sent: list[tuple[str, dict[str, object]]] = []
    current: dict[str, str] = {}

    def handle(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/contents/vault/Private/enikk/선호/배포 알림.md")
        if request.method == "GET":
            if "sha" not in current:
                return httpx.Response(404, json={"message": "Not Found"})
            return httpx.Response(200, json={"sha": current["sha"]})
        body = json.loads(request.content)
        sent.append((request.method, body))
        current["sha"] = f"blob{len(sent)}"
        return httpx.Response(201, json={"content": {"sha": current["sha"]}})

    writer = VaultWriter(_app(httpx.MockTransport(handle)))
    assert await writer.put(TOKEN, PATH, "첫 내용", "memory: remember x [skip ci]") == "blob1"
    assert await writer.put(TOKEN, PATH, "둘째", "memory: update x [skip ci]") == "blob2"
    (_, first), (_, second) = sent
    assert "sha" not in first
    assert second["sha"] == "blob1"
    assert base64.b64decode(str(second["content"])).decode() == "둘째"
    assert second["branch"] == "main"


async def test_a_concurrent_owner_push_is_retried_once() -> None:
    puts = 0

    def handle(request: httpx.Request) -> httpx.Response:
        nonlocal puts
        if request.method == "GET":
            return httpx.Response(200, json={"sha": f"head{puts}"})
        puts += 1
        if puts == 1:
            return httpx.Response(409, json={"message": "sha does not match"})
        return httpx.Response(200, json={"content": {"sha": "new"}})

    writer = VaultWriter(_app(httpx.MockTransport(handle)))
    assert await writer.put(TOKEN, PATH, "x", "m") == "new"
    assert puts == 2


async def test_reader_lists_blobs_and_decodes_text() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        if "/git/trees/" in request.url.path:
            return httpx.Response(
                200,
                json={
                    "tree": [
                        {"path": "vault", "type": "tree", "sha": "t"},
                        {"path": PATH, "type": "blob", "sha": "b1"},
                    ]
                },
            )
        encoded = base64.b64encode("# 제목\n본문".encode()).decode()
        return httpx.Response(200, json={"content": encoded, "encoding": "base64"})

    reader = VaultReader(_app(httpx.MockTransport(handle)))
    [file] = await reader.files(TOKEN)
    assert (file.path, file.sha) == (PATH, "b1")
    assert await reader.text(TOKEN, "b1") == "# 제목\n본문"
