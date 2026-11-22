from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest
from pydantic import SecretStr

from enikk_github.app import GitHubApp, GitHubAppConfig, InstallationToken
from enikk_github.pulls import CheckRun, PullRequests
from enikk_kernel.testing import ManualClock

pytestmark = pytest.mark.anyio

CONFIG = GitHubAppConfig(app_id=5869950, installation_id=184545127, owner="404Vector")
TOKEN = InstallationToken(
    token=SecretStr("ghs_watch"),
    expires_at=datetime(2026, 11, 22, 9, tzinfo=UTC),
    repository="project-enikk",
    permissions={"checks": "read"},
)


async def test_only_the_newest_run_of_each_check_counts() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/repos/404Vector/project-enikk/commits/abc/check-runs"
        return httpx.Response(
            200,
            json={
                "check_runs": [
                    {
                        "id": 12,
                        "name": "protected-paths",
                        "status": "completed",
                        "conclusion": "success",
                    },
                    {"id": 13, "name": "checks", "status": "completed", "conclusion": "success"},
                    {
                        "id": 11,
                        "name": "protected-paths",
                        "status": "completed",
                        "conclusion": "cancelled",
                    },
                ]
            },
        )

    app = GitHubApp.create(
        CONFIG, lambda: None, ManualClock(), transport=httpx.MockTransport(handler)
    )
    runs = await PullRequests(app).check_runs(TOKEN, "abc")
    assert runs == [
        CheckRun(name="checks", completed=True, conclusion="success"),
        CheckRun(name="protected-paths", completed=True, conclusion="success"),
    ]
    assert not any(r.failed for r in runs)
