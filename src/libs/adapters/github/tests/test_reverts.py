from __future__ import annotations

import json
from datetime import UTC, datetime

import httpx
import pytest
from pydantic import SecretStr

from enikk_github.app import GitHubApp, GitHubAppConfig, InstallationToken
from enikk_github.reverts import Reverter, RevertPull, RevertRefused
from enikk_kernel.testing import ManualClock

pytestmark = pytest.mark.anyio

CONFIG = GitHubAppConfig(app_id=5869950, installation_id=184545127, owner="404Vector")
TOKEN = InstallationToken(
    token=SecretStr("ghs_dev"),
    expires_at=datetime(2026, 11, 22, 9, tzinfo=UTC),
    repository="project-enikk",
    permissions={"contents": "write"},
)
REPO = "/repos/404Vector/project-enikk"
PARENT, TARGET, HEAD = "1" * 40, "2" * 40, "3" * 40


def _blob(path: str, sha: str, mode: str = "100644") -> dict[str, str]:
    return {"path": path, "mode": mode, "type": "blob", "sha": sha}


class FakeGitHub:
    """Commit TARGET changed a.py and added new.py; HEAD changed only other.py since."""

    def __init__(self, head_a: str = "a2") -> None:
        self.trees = {
            PARENT: [_blob("a.py", "a1", "100755"), _blob("other.py", "o1")],
            TARGET: [_blob("a.py", "a2", "100755"), _blob("new.py", "n1"), _blob("other.py", "o1")],
            HEAD: [
                _blob("a.py", head_a, "100755"),
                _blob("new.py", "n1"),
                _blob("other.py", "o2"),
            ],
        }
        self.posted: dict[str, object] = {}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix(REPO)
        if request.method == "GET" and path == "/pulls":
            return httpx.Response(200, json=[])
        if path == f"/commits/{TARGET}":
            return httpx.Response(
                200,
                json={
                    "sha": TARGET,
                    "commit": {"message": "feat: break things (#9)\n\nbody"},
                    "parents": [{"sha": PARENT}],
                    "files": [{"filename": "a.py"}, {"filename": "new.py"}],
                },
            )
        if path == "/git/ref/heads/main":
            return httpx.Response(200, json={"object": {"sha": HEAD}})
        if path.startswith("/git/commits/"):
            commit = path.rsplit("/", 1)[1]
            return httpx.Response(200, json={"sha": commit, "tree": {"sha": f"tree-{commit}"}})
        if path.startswith("/git/trees/tree-"):
            commit = path.rsplit("-", 1)[1]
            return httpx.Response(200, json={"sha": "t", "tree": self.trees[commit]})
        if request.method == "POST":
            self.posted[path] = json.loads(request.content)
            if path == "/pulls":
                return httpx.Response(201, json={"number": 50, "html_url": "https://x/50"})
            return httpx.Response(201, json={"sha": f"new{len(self.posted)}"})
        raise AssertionError(f"unexpected {request.method} {path}")


def _reverter(github: FakeGitHub) -> Reverter:
    transport = httpx.MockTransport(github)
    return Reverter(GitHubApp.create(CONFIG, lambda: None, ManualClock(), transport=transport))


async def test_revert_restores_the_parent_content_on_top_of_head() -> None:
    github = FakeGitHub()
    result = await _reverter(github).open_revert(TOKEN, TARGET)
    assert result == RevertPull(number=50, url="https://x/50", branch=f"agent/revert-{TARGET[:12]}")
    tree = github.posted["/git/trees"]
    assert tree == {
        "base_tree": f"tree-{HEAD}",
        "tree": [
            {"path": "a.py", "mode": "100755", "type": "blob", "sha": "a1"},
            {"path": "new.py", "mode": "100644", "type": "blob", "sha": None},
        ],
    }
    commit = github.posted["/git/commits"]
    assert isinstance(commit, dict)
    assert commit["parents"] == [HEAD]
    assert commit["message"].startswith('Revert "feat: break things (#9)"')
    assert github.posted["/git/refs"] == {
        "ref": f"refs/heads/agent/revert-{TARGET[:12]}",
        "sha": "new2",
    }
    pull = github.posted["/pulls"]
    assert isinstance(pull, dict)
    assert pull["base"] == "main"


async def test_later_changes_to_the_same_file_refuse_the_revert() -> None:
    github = FakeGitHub(head_a="a3")
    result = await _reverter(github).open_revert(TOKEN, TARGET)
    assert isinstance(result, RevertRefused)
    assert result.conflicts == ("a.py",)
    assert github.posted == {}
