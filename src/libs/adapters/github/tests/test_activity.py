from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest
from pydantic import SecretStr

from enikk_github.activity import RepositoryActivity
from enikk_github.app import GitHubApp, GitHubAppConfig, InstallationToken
from enikk_kernel.testing import ManualClock

pytestmark = pytest.mark.anyio

CONFIG = GitHubAppConfig(app_id=5869950, installation_id=184545127, owner="404Vector")
TOKEN = InstallationToken(
    token=SecretStr("ghs_watch"),
    expires_at=datetime(2026, 11, 22, 9, tzinfo=UTC),
    repository="project-enikk",
    permissions={"actions": "read"},
)
SHA = "e" * 40


def _activity(handler: httpx.MockTransport) -> RepositoryActivity:
    return RepositoryActivity(
        GitHubApp.create(CONFIG, lambda: None, ManualClock(), transport=handler)
    )


async def test_workflow_runs_of_main() -> None:
    seen: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        run = {
            "id": 9,
            "head_sha": SHA,
            "status": "completed",
            "conclusion": "failure",
            "html_url": "https://github.com/404Vector/project-enikk/actions/runs/9",
            "created_at": "2026-11-22T09:00:00Z",
        }
        return httpx.Response(200, json={"total_count": 1, "workflow_runs": [run]})

    [run] = await _activity(httpx.MockTransport(handle)).workflow_runs(TOKEN, "images.yml")
    assert seen[0].url.path == "/repos/404Vector/project-enikk/actions/workflows/images.yml/runs"
    assert seen[0].url.params["branch"] == "main"
    assert run.build_failed
    assert run.head_sha == SHA


@pytest.mark.parametrize(("conclusion", "failed"), [("cancelled", False), ("timed_out", True)])
async def test_cancelled_builds_are_not_failures(conclusion: str, failed: bool) -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        run = {
            "id": 1,
            "head_sha": SHA,
            "status": "completed",
            "conclusion": conclusion,
            "html_url": "https://x",
            "created_at": "2026-11-22T09:00:00Z",
        }
        return httpx.Response(200, json={"workflow_runs": [run]})

    [run] = await _activity(httpx.MockTransport(handle)).workflow_runs(TOKEN, "images.yml")
    assert run.build_failed is failed


async def test_pull_of_commit_is_the_merged_one() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        assert request.url.path == f"/repos/404Vector/project-enikk/commits/{SHA}/pulls"
        return httpx.Response(
            200,
            json=[
                {"number": 3, "title": "open", "html_url": "https://x/3", "merged_at": None},
                {
                    "number": 4,
                    "title": "feat: merged",
                    "html_url": "https://x/4",
                    "merged_at": "2026-11-22T08:00:00Z",
                },
            ],
        )

    pull = await _activity(httpx.MockTransport(handle)).pull_of_commit(TOKEN, SHA)
    assert pull is not None
    assert (pull.number, pull.title) == (4, "feat: merged")
