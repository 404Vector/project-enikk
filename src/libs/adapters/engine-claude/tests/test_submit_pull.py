"""``submit_pull_request``: the worktree's commits go to core (ADR-0037 §6b)."""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from enikk_contracts.agents import PlatformTool, ToolPolicy
from enikk_contracts.pulls import PullSubmitted
from enikk_core_client.errors import CoreConflictError, CoreUnavailableError
from enikk_engine_claude.platform_tools import (
    SUBMIT_PULL_TOOL,
    granted_platform_tools,
    submit_pull_tool,
)
from enikk_runtime.model import EngineJob, ResolvedPrompt, SessionWorkspace
from enikk_runtime.testing import definition
from enikk_runtime.workspace import WorkspaceError

pytestmark = pytest.mark.anyio

SESSION = uuid.UUID(int=3)


def _workspace(*, worktree: bool = True) -> SessionWorkspace:
    return SessionWorkspace(
        repository="404Vector/project-enikk",
        repo_dir=Path("/data/repo.git"),
        snapshot_dir=None,
        commit=None,
        fetched_at=None,
        fresh=True,
        worktree_dir=Path("/data/worktrees") / str(SESSION) if worktree else None,
        branch=f"session/{SESSION}" if worktree else None,
        tmp_dir=Path("/data/tmp") / str(SESSION),
    )


def _job(*, grant: bool = True, workspace: SessionWorkspace | None = None) -> EngineJob:
    tools = ToolPolicy(platform=(PlatformTool.PULL_REQUESTS,) if grant else ())
    return EngineJob(
        task_id=uuid.uuid4(),
        lease_id=uuid.uuid4(),
        attempt=1,
        session_id=SESSION,
        engine_session_id=None,
        definition=definition().model_copy(update={"tools": tools}),
        prompt=ResolvedPrompt(name="assistant", version=1, text="hi", model="claude-test-1"),
        input="hi",
        workspace=workspace,
    )


class _Pulls:
    def __init__(self, error: Exception | None = None) -> None:
        self.calls: list[tuple[str | None, str | None]] = []
        self._error = error

    async def submit(self, job: EngineJob, title: str | None, body: str | None) -> PullSubmitted:
        self.calls.append((title, body))
        if self._error is not None:
            raise self._error
        return PullSubmitted(
            number=81,
            url="https://github.com/404Vector/project-enikk/pull/81",
            branch=f"agent/{SESSION}",
            head="c" * 40,
            commits=1,
            created=True,
        )


def test_the_tool_needs_the_grant_and_a_worktree() -> None:
    assert SUBMIT_PULL_TOOL in granted_platform_tools(_job(workspace=_workspace()), pulls=True)
    assert SUBMIT_PULL_TOOL not in granted_platform_tools(_job(workspace=_workspace()))
    assert SUBMIT_PULL_TOOL not in granted_platform_tools(
        _job(grant=False, workspace=_workspace()), pulls=True
    )
    assert SUBMIT_PULL_TOOL not in granted_platform_tools(
        _job(workspace=_workspace(worktree=False)), pulls=True
    )
    assert SUBMIT_PULL_TOOL not in granted_platform_tools(_job(), pulls=True)


async def test_the_tool_submits_and_reports_the_pr() -> None:
    pulls = _Pulls()
    tool = submit_pull_tool(_job(workspace=_workspace()), pulls)  # type: ignore[arg-type]
    result = await tool.handler({"title": "docs: a", "body": "본문"})
    assert not result["is_error"]
    assert "PR #81 opened" in result["content"][0]["text"]
    assert pulls.calls == [("docs: a", "본문")]


@pytest.mark.parametrize(
    ("error", "message"),
    [
        (WorkspaceError("there are no commits after main"), "Nothing was submitted"),
        (CoreUnavailableError("down"), "unreachable"),
        (CoreConflictError("merge commit"), "Core refused"),
    ],
)
async def test_failures_are_told_to_the_model(error: Exception, message: str) -> None:
    tool = submit_pull_tool(_job(workspace=_workspace()), _Pulls(error))  # type: ignore[arg-type]
    result = await tool.handler({})
    assert result["is_error"]
    assert message in result["content"][0]["text"]
