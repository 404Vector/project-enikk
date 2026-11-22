from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pydantic_core
import pytest
from pydantic import ValidationError

from enikk_contracts.credentials import CredentialName
from enikk_core.github import GitHubHealthCheck, load_repository_policy
from enikk_core.settings import CoreSettings, RoleBindingSettings
from enikk_core_client import CoreForbiddenError
from enikk_core_domain.repositories import RepositoryPolicy
from enikk_github.permissions import TokenPurpose
from enikk_kernel.errors import ConfigurationError

if TYPE_CHECKING:
    from conftest import Harness

REPO_ROOT = Path(__file__).resolve().parents[4]


@pytest.mark.anyio
async def test_smoke_sees_a_healthy_installation(harness: Harness) -> None:
    async with harness.client("smoke") as client:
        health = await client.github_health()
    assert health.ok
    assert [r.name for r in health.repositories] == ["project-enikk", "side-project"]
    assert harness.github.issued == [
        ("project-enikk", TokenPurpose.WATCH),
        ("side-project", TokenPurpose.WATCH),
    ]
    assert harness.github.revoked == ["project-enikk", "side-project"]
    assert "ghs-" not in health.model_dump_json()


@pytest.mark.anyio
async def test_missing_permissions_and_unreachable_repositories_are_reported(
    harness: Harness,
) -> None:
    harness.github.permissions["statuses"] = "read"
    del harness.github.permissions["administration"]
    harness.github.unreachable.add("side-project")
    async with harness.client("smoke") as client:
        health = await client.github_health()
    assert not health.ok
    gaps = {g.permission: (g.required, g.granted) for g in health.gaps}
    assert gaps == {"administration": ("write", None), "statuses": ("write", "read")}
    assert [r.reachable for r in health.repositories] == [True, False]


@pytest.mark.anyio
@pytest.mark.parametrize("token", ["slack", "claude", "stranger"])
async def test_only_the_smoke_identity_reads_the_github_status(
    harness: Harness, token: str
) -> None:
    async with harness.client(token) as client:
        with pytest.raises(CoreForbiddenError):
            await client.github_health()


@pytest.mark.anyio
async def test_unconfigured_app_and_missing_key_are_reported(harness: Harness) -> None:
    policy = load_repository_policy(
        REPO_ROOT / "config/github.yaml", self_repository="project-enikk"
    )
    unconfigured = await GitHubHealthCheck(None, policy, lambda: None).check()
    assert (unconfigured.configured, unconfigured.key_registered) == (False, False)
    no_key = await GitHubHealthCheck(harness.github, policy, lambda: None).check()
    assert (no_key.configured, no_key.key_registered, no_key.ok) == (True, False, False)
    assert harness.github.issued == []


def test_repository_list_in_git_is_valid() -> None:
    policy = load_repository_policy(
        REPO_ROOT / "config/github.yaml", self_repository="project-enikk"
    )
    assert policy.own.name == "project-enikk"
    assert not policy.own.workflows


def test_self_repository_must_match_core_settings(tmp_path: Path) -> None:
    path = tmp_path / "github.yaml"
    path.write_text("repositories:\n  - name: other\n    self: true\n")
    with pytest.raises(ConfigurationError, match="must be project-enikk"):
        load_repository_policy(path, self_repository="project-enikk")


@pytest.mark.parametrize(
    ("document", "message"),
    [
        ({"repositories": [{"name": "a", "self": True, "workflows": True}]}, "cannot allow"),
        ({"repositories": [{"name": "a"}]}, "exactly one"),
        ({"repositories": [{"name": "a", "self": True}, {"name": "A"}]}, "unique"),
        ({"repositories": [{"name": "a/b", "self": True}]}, "pattern"),
    ],
)
def test_repository_policy_rules(document: object, message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        RepositoryPolicy.model_validate_json(pydantic_core.to_json(document))


def test_the_app_key_cannot_be_granted_to_a_service() -> None:
    with pytest.raises(ValidationError, match="never leave core"):
        RoleBindingSettings(
            namespace="enikk",
            service_account="enikk-dev-agent",
            role="dev-agent",
            credentials=(CredentialName.GITHUB_APP_PRIVATE_KEY,),
        )


def test_github_is_off_without_both_ids(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENIKK_CORE_GITHUB__APP_ID", "5869950")
    assert CoreSettings().github.to_config() is None
    monkeypatch.setenv("ENIKK_CORE_GITHUB__INSTALLATION_ID", "184545127")
    config = CoreSettings().github.to_config()
    assert config is not None
    assert (config.app_id, config.installation_id) == (5869950, 184545127)
