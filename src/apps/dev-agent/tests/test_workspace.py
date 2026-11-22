from __future__ import annotations

import os
import stat
import uuid
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest
import yaml
from pydantic import SecretStr

from enikk_contracts.agents import AgentDefinition, Execution, PromptRef, ToolPolicy
from enikk_contracts.common import Engine
from enikk_contracts.github import GitHubToken
from enikk_contracts.tasks import TaskAssignment
from enikk_dev_agent.app import task_brief
from enikk_dev_agent.workspace import GitWorkspace, WorkspaceError
from enikk_kernel.testing import RecordingRunner

if TYPE_CHECKING:
    from pathlib import Path

TASK = uuid.UUID("12345678-1234-5678-1234-567812345678")
BRANCH = f"agent/{TASK}"
SECRET = "ghs_secret-token-value"


def _token(*, workflows: bool = False) -> GitHubToken:
    return GitHubToken(
        token=SecretStr(SECRET),
        owner="404Vector",
        repository="project-enikk",
        expires_at=datetime(2026, 1, 1, tzinfo=UTC),
        workflows=workflows,
        author_name="enikk-deployer[bot]",
        author_email="42+enikk-deployer[bot]@users.noreply.github.com",
    )


@pytest.fixture
def isolated_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("GIT_CONFIG_GLOBAL", "GH_CONFIG_DIR", "UV_CACHE_DIR", "XDG_CACHE_HOME"):
        monkeypatch.delenv(name, raising=False)


@pytest.mark.usefixtures("isolated_env")
def test_token_lives_only_in_the_gh_config_file(tmp_path: Path) -> None:
    runner = RecordingRunner()
    tools = tmp_path / "tools"
    workspace = GitWorkspace(runner, tmp_path / "ws", tools).prepare(_token(), BRANCH)

    assert workspace.branch == f"agent/{TASK}"
    assert workspace.repo_dir == tmp_path / "ws" / "repo"
    assert runner.argvs == [
        (
            "git",
            "clone",
            "--depth",
            "100",
            "https://github.com/404Vector/project-enikk.git",
            str(tmp_path / "ws" / "repo"),
        ),
        ("git", "ls-remote", "--heads", "origin", BRANCH),
        ("git", "switch", "--create", BRANCH),
    ]
    assert not workspace.continuing
    assert all(SECRET not in " ".join(argv) for argv in runner.argvs)
    assert all(SECRET not in value for value in workspace.tool_env.values())
    assert all(SECRET not in value for value in os.environ.values())

    hosts = tools / "gh" / "hosts.yml"
    assert stat.S_IMODE(hosts.stat().st_mode) == 0o600
    assert yaml.safe_load(hosts.read_text())["github.com"]["oauth_token"] == SECRET
    gitconfig = (tools / "gitconfig").read_text()
    assert "helper = !gh auth git-credential" in gitconfig
    assert "42+enikk-deployer[bot]@users.noreply.github.com" in gitconfig
    assert SECRET not in gitconfig
    assert os.environ["GIT_CONFIG_GLOBAL"] == str(tools / "gitconfig")


@pytest.mark.usefixtures("isolated_env")
def test_cleanup_revokes_and_removes_credentials(tmp_path: Path) -> None:
    runner = RecordingRunner()
    tools = tmp_path / "tools"
    git = GitWorkspace(runner, tmp_path / "ws", tools)
    git.prepare(_token(), BRANCH)
    git.revoke_and_forget()
    assert runner.argvs[-1] == ("gh", "api", "--method", "DELETE", "/installation/token")
    assert not tools.exists()


@pytest.mark.usefixtures("isolated_env")
def test_cleanup_without_credentials_calls_nothing(tmp_path: Path) -> None:
    runner = RecordingRunner()
    GitWorkspace(runner, tmp_path / "ws", tmp_path / "tools").revoke_and_forget()
    assert runner.argvs == []


@pytest.mark.usefixtures("isolated_env")
def test_failed_clone_is_a_workspace_error(tmp_path: Path) -> None:
    runner = RecordingRunner()
    runner.when("git", "clone", returncode=128, stderr="fatal: repository not found")
    with pytest.raises(WorkspaceError, match="repository not found"):
        GitWorkspace(runner, tmp_path / "ws", tmp_path / "tools").prepare(_token(), BRANCH)


@pytest.mark.usefixtures("isolated_env")
def test_brief_states_where_the_agent_works(tmp_path: Path) -> None:
    workspace = GitWorkspace(RecordingRunner(), tmp_path / "ws", tmp_path / "tools").prepare(
        _token(), BRANCH
    )
    brief = task_brief(_assignment(), _token(), workspace)
    assert "404Vector/project-enikk" in brief
    assert BRANCH in brief
    assert "new branch from main" in brief
    assert "must not change files under .github/workflows" in brief
    assert brief.endswith("Add a line to the README")
    assert SECRET not in brief


@pytest.mark.usefixtures("isolated_env")
def test_an_existing_branch_is_continued(tmp_path: Path) -> None:
    runner = RecordingRunner()
    runner.when("git", "ls-remote", stdout=f"abc123\trefs/heads/{BRANCH}\n")
    workspace = GitWorkspace(runner, tmp_path / "ws", tmp_path / "tools").prepare(_token(), BRANCH)
    assert workspace.continuing
    assert runner.argvs[-2:] == [
        (
            "git",
            "fetch",
            "--depth",
            "100",
            "origin",
            f"+refs/heads/{BRANCH}:refs/remotes/origin/{BRANCH}",
        ),
        ("git", "switch", "--create", BRANCH, "--track", f"origin/{BRANCH}"),
    ]
    brief = task_brief(_assignment(), _token(), workspace)
    assert "do not open another pull request" in brief


def test_the_work_branch_follows_the_session() -> None:
    from enikk_dev_agent.app import work_branch

    assignment = _assignment()
    assert work_branch(assignment) == f"agent/{assignment.session_id}"


def _assignment() -> TaskAssignment:
    return TaskAssignment(
        task_id=TASK,
        lease_id=uuid.uuid4(),
        lease_expires_at=datetime(2026, 1, 1, tzinfo=UTC) + timedelta(minutes=2),
        attempt=1,
        definition=AgentDefinition(
            name="developer",
            description="dev",
            engine=Engine.CLAUDE,
            execution=Execution.ISOLATED,
            prompt=PromptRef(name="developer", label="production"),
            tools=ToolPolicy(builtin=("Bash",)),
        ),
        session_id=uuid.UUID("87654321-4321-8765-4321-876543218765"),
        engine_session_id=None,
        input="Add a line to the README",
    )
