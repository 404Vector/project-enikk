from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, cast

import pytest

from enikk_contracts.github import GitHubHealth, PermissionGap, RepositoryCheck
from enikk_kernel.testing import RecordingRunner
from enikk_ops.settings import SmokeSettings
from enikk_ops.smoke import ClaudeSmokeTest, CoreSmokeTest, SmokeTestError
from enikk_platform.adapters import KubectlCli
from enikk_platform.domain import KubeTarget

if TYPE_CHECKING:
    from pydantic import SecretStr

    from enikk_core_client import CoreClient


class _UnreachableError(Exception):
    pass


def _refuse(base_url: str, token: SecretStr) -> CoreClient:
    raise _UnreachableError(base_url)


def test_smoke_issues_tokens_and_forwards_without_creating_resources() -> None:
    runner = RecordingRunner()
    runner.when("kubectl", stdout="token-value\n")
    smoke = CoreSmokeTest(
        SmokeSettings(), "docker-desktop", KubectlCli(runner), client=_refuse, report=lambda _: None
    )
    with pytest.raises(_UnreachableError, match=r"127\.0\.0\.1:18000"):
        smoke.run()

    created = [argv for argv in runner.argvs if "create" in argv]
    assert [argv[argv.index("token") + 1] for argv in created] == [
        "enikk-smoke",
        "enikk-runtime-claude",
        "default",
    ]
    for argv in created:
        assert argv[argv.index("--audience") + 1] == "enikk-core"
    assert all(not call.mutating for call in runner.calls)
    assert runner.background_calls == [
        (
            "kubectl",
            "--context",
            "docker-desktop",
            "--namespace",
            "enikk",
            "port-forward",
            "svc/enikk-core",
            "18000:80",
        )
    ]


def test_create_token_returns_secret() -> None:
    runner = RecordingRunner()
    runner.when("kubectl", stdout="abc\n")
    token = KubectlCli(runner).create_token(
        KubeTarget(context="c", namespace="enikk"), "sa", audience="enikk-core"
    )
    assert token.get_secret_value() == "abc"
    assert "abc" not in repr(token)


def test_smoke_error_is_project_error() -> None:
    assert SmokeTestError("x").exit_code == 1


class _GitHubStatus:
    def __init__(self, health: GitHubHealth) -> None:
        self._health = health

    async def github_health(self) -> GitHubHealth:
        return self._health


def _check(health: GitHubHealth) -> list[str]:
    lines: list[str] = []
    smoke = CoreSmokeTest(
        SmokeSettings(), "docker-desktop", KubectlCli(RecordingRunner()), report=lines.append
    )
    asyncio.run(smoke.check_github(cast("CoreClient", _GitHubStatus(health))))
    return lines


def test_github_check_skips_until_the_key_is_registered() -> None:
    lines = _check(GitHubHealth(configured=True, key_registered=False))
    assert "not registered, skipped" in lines[0]


def test_github_check_passes_with_every_permission() -> None:
    health = GitHubHealth(
        configured=True,
        key_registered=True,
        repositories=(RepositoryCheck(name="project-enikk", reachable=True),),
    )
    assert "reaches project-enikk" in _check(health)[0]


def test_github_check_fails_on_a_missing_permission() -> None:
    health = GitHubHealth(
        configured=True,
        key_registered=True,
        gaps=(PermissionGap(permission="statuses", required="write", granted="read"),),
    )
    lines: list[str] = []
    smoke = CoreSmokeTest(
        SmokeSettings(), "docker-desktop", KubectlCli(RecordingRunner()), report=lines.append
    )
    with pytest.raises(SmokeTestError, match="lacks permissions"):
        asyncio.run(smoke.check_github(cast("CoreClient", _GitHubStatus(health))))
    assert "statuses: needs write, granted read" in lines[0]


def test_claude_smoke_uses_only_the_connector_token() -> None:
    runner = RecordingRunner()
    runner.when("kubectl", stdout="token-value\n")
    smoke = ClaudeSmokeTest(
        SmokeSettings(), "docker-desktop", KubectlCli(runner), client=_refuse, report=lambda _: None
    )
    with pytest.raises(_UnreachableError):
        smoke.run()
    created = [argv for argv in runner.argvs if "create" in argv]
    assert [argv[argv.index("token") + 1] for argv in created] == ["enikk-smoke"]
    assert all(not call.mutating for call in runner.calls)
