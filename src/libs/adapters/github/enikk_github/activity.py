"""Read-only repository activity for core's deployment watcher (ADR-0023)."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Final

from pydantic import BaseModel, ConfigDict, TypeAdapter, ValidationError

from enikk_core_domain.errors import DependencyUnavailableError
from enikk_kernel.models import FrozenModel

if TYPE_CHECKING:
    import httpx

    from enikk_github.app import GitHubApp, InstallationToken

FAILED_BUILDS: Final = frozenset({"failure", "timed_out", "startup_failure"})
"""Conclusions that mean a build did not produce images. ``cancelled`` is not one:
a newer push to main cancels the older build on purpose."""


class _Body(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")


class _Run(_Body):
    id: int
    head_sha: str
    status: str
    conclusion: str | None = None
    html_url: str
    created_at: datetime


class _Runs(_Body):
    workflow_runs: tuple[_Run, ...] = ()


class _Head(_Body):
    ref: str


class _CommitPull(_Body):
    number: int
    title: str
    html_url: str
    head: _Head
    merged_at: datetime | None = None


_RUNS: Final = TypeAdapter(_Runs)
_COMMIT_PULLS: Final = TypeAdapter(tuple[_CommitPull, ...])


class WorkflowRun(FrozenModel):
    id: int
    head_sha: str
    completed: bool
    conclusion: str | None
    url: str
    created_at: datetime

    @property
    def build_failed(self) -> bool:
        return self.completed and self.conclusion in FAILED_BUILDS


class CommitPull(FrozenModel):
    number: int
    title: str
    url: str
    branch: str


class RepositoryActivity:
    def __init__(self, app: GitHubApp) -> None:
        self._app = app

    def _repo(self, token: InstallationToken) -> str:
        return f"/repos/{self._app.owner}/{token.repository}"

    async def workflow_runs(
        self, token: InstallationToken, workflow: str, branch: str = "main"
    ) -> list[WorkflowRun]:
        """The most recent runs of ``workflow`` (its file name) on ``branch``."""
        response = await self._app.call(
            "GET",
            f"{self._repo(token)}/actions/workflows/{workflow}/runs",
            token,
            params={"branch": branch, "event": "push", "per_page": "20"},
        )
        return [
            WorkflowRun(
                id=r.id,
                head_sha=r.head_sha,
                completed=r.status == "completed",
                conclusion=r.conclusion,
                url=r.html_url,
                created_at=r.created_at,
            )
            for r in _parse(_RUNS, response).workflow_runs
        ]

    async def pull_of_commit(self, token: InstallationToken, sha: str) -> CommitPull | None:
        """The merged pull request that produced ``sha`` on main, if any."""
        response = await self._app.call("GET", f"{self._repo(token)}/commits/{sha}/pulls", token)
        merged = [p for p in _parse(_COMMIT_PULLS, response) if p.merged_at is not None]
        if not merged:
            return None
        pull = merged[0]
        return CommitPull(
            number=pull.number, title=pull.title, url=pull.html_url, branch=pull.head.ref
        )


def _parse[T](adapter: TypeAdapter[T], response: httpx.Response) -> T:
    try:
        return adapter.validate_json(response.content)
    except ValidationError:
        raise DependencyUnavailableError("GitHub returned an unexpected body") from None
