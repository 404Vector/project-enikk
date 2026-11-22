"""Pull request operations for core's approval flow (ADR-0020), on one repository.

Every call takes the installation token it acts with, so the caller decides which
purpose (and so which permissions) each step uses (ADR-0019).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Final

import httpx  # noqa: TC002 - used in a runtime annotation of a helper
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError

from enikk_core_domain.errors import DependencyUnavailableError
from enikk_github.app import GitHubRequestError
from enikk_kernel.models import FrozenModel

if TYPE_CHECKING:
    from enikk_github.app import GitHubApp, InstallationToken

APPROVAL_CONTEXT: Final = "enikk/owner-approval"
DIFF_LIMIT: Final = 400_000
FAILING: Final = frozenset({"failure", "cancelled", "timed_out", "action_required", "stale"})


class _Body(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")


class _User(_Body):
    login: str


class _Repo(_Body):
    full_name: str


class _Ref(_Body):
    sha: str
    ref: str
    repo: _Repo | None = None


class _Pull(_Body):
    number: int
    title: str
    html_url: str
    body: str | None = None
    draft: bool = False
    state: str
    merged: bool = False
    mergeable_state: str | None = None
    user: _User
    head: _Ref
    base: _Ref


class _CheckRun(_Body):
    id: int
    name: str
    status: str
    conclusion: str | None = None


class _CheckRuns(_Body):
    check_runs: tuple[_CheckRun, ...] = ()


class _File(_Body):
    filename: str
    additions: int = 0
    deletions: int = 0


class _Merge(_Body):
    merged: bool = False
    message: str = ""


_PULLS: TypeAdapter[tuple[_Pull, ...]] = TypeAdapter(tuple[_Pull, ...])
_FILES: TypeAdapter[tuple[_File, ...]] = TypeAdapter(tuple[_File, ...])
_PULL: TypeAdapter[_Pull] = TypeAdapter(_Pull)
_CHECKS: TypeAdapter[_CheckRuns] = TypeAdapter(_CheckRuns)
_MERGE: TypeAdapter[_Merge] = TypeAdapter(_Merge)


class PullInfo(FrozenModel):
    number: Annotated[int, Field(ge=1)]
    title: str
    url: str
    body: str
    draft: bool
    open: bool
    merged: bool
    author: str
    head_sha: str
    branch: str
    from_fork: bool
    mergeable_state: str | None = None
    """Only on a single-PR read; ``behind`` means main moved on since the branch."""


class CheckRun(FrozenModel):
    name: str
    completed: bool
    conclusion: str | None

    @property
    def failed(self) -> bool:
        return self.completed and self.conclusion in FAILING


class FileChange(FrozenModel):
    path: str
    additions: int
    deletions: int


class MergeResult(FrozenModel):
    merged: bool
    detail: str = ""


class _StatusBody(FrozenModel):
    state: str
    context: str
    description: str


class _MergeBody(FrozenModel):
    sha: str
    merge_method: str
    commit_title: str


class _UpdateBranchBody(FrozenModel):
    expected_head_sha: str


class _CloseBody(FrozenModel):
    state: str


class PullRequests:
    """Pull requests of the repository a token is scoped to, through the GitHub App."""

    def __init__(self, app: GitHubApp) -> None:
        self._app = app

    def _repo(self, token: InstallationToken) -> str:
        return f"/repos/{self._app.owner}/{token.repository}"

    async def open_pulls(self, token: InstallationToken, base: str = "main") -> list[PullInfo]:
        response = await self._app.call(
            "GET",
            f"{self._repo(token)}/pulls",
            token,
            params={"state": "open", "base": base, "per_page": "50"},
        )
        return [_pull(p) for p in _parse(_PULLS, response)]

    async def get(self, token: InstallationToken, number: int) -> PullInfo:
        response = await self._app.call("GET", f"{self._repo(token)}/pulls/{number}", token)
        return _pull(_parse(_PULL, response))

    async def check_runs(self, token: InstallationToken, sha: str) -> list[CheckRun]:
        response = await self._app.call(
            "GET",
            f"{self._repo(token)}/commits/{sha}/check-runs",
            token,
            params={"per_page": "100"},
        )
        # A check re-runs when its workflow fires again on the same commit (for
        # example after the PR description is edited) and the older run is
        # cancelled. Only the newest run of each check counts.
        latest: dict[str, _CheckRun] = {}
        for run in _parse(_CHECKS, response).check_runs:
            if run.name not in latest or run.id > latest[run.name].id:
                latest[run.name] = run
        return [
            CheckRun(name=r.name, completed=r.status == "completed", conclusion=r.conclusion)
            for r in sorted(latest.values(), key=lambda r: r.name)
        ]

    async def files(self, token: InstallationToken, number: int) -> list[FileChange]:
        response = await self._app.call(
            "GET",
            f"{self._repo(token)}/pulls/{number}/files",
            token,
            params={"per_page": "100"},
        )
        return [
            FileChange(path=f.filename, additions=f.additions, deletions=f.deletions)
            for f in _parse(_FILES, response)
        ]

    async def diff(self, token: InstallationToken, number: int) -> tuple[str, bool]:
        """The unified diff, cut at ``DIFF_LIMIT`` characters. Returns (diff, truncated)."""
        response = await self._app.call(
            "GET",
            f"{self._repo(token)}/pulls/{number}",
            token,
            accept="application/vnd.github.diff",
        )
        text = response.text
        return text[:DIFF_LIMIT], len(text) > DIFF_LIMIT

    async def approve(self, token: InstallationToken, sha: str) -> None:
        """Record the owner's approval on exactly this commit (I-33)."""
        body = _StatusBody(
            state="success", context=APPROVAL_CONTEXT, description="Approved by the owner in Slack"
        )
        await self._app.call(
            "POST", f"{self._repo(token)}/statuses/{sha}", token, body=body.model_dump_json()
        )

    async def merge(self, token: InstallationToken, pull: PullInfo) -> MergeResult:
        """Squash-merge, only while the head is still ``pull.head_sha``."""
        body = _MergeBody(
            sha=pull.head_sha, merge_method="squash", commit_title=f"{pull.title} (#{pull.number})"
        )
        try:
            response = await self._app.call(
                "PUT",
                f"{self._repo(token)}/pulls/{pull.number}/merge",
                token,
                body=body.model_dump_json(),
            )
        except GitHubRequestError as exc:
            if exc.status in {405, 409, 422}:
                return MergeResult(merged=False, detail=exc.message or f"HTTP {exc.status}")
            raise
        result = _parse(_MERGE, response)
        return MergeResult(merged=result.merged, detail=result.message)

    async def update_branch(self, token: InstallationToken, pull: PullInfo) -> bool:
        """Merge main into the PR branch, only while its head is ``pull.head_sha``."""
        body = _UpdateBranchBody(expected_head_sha=pull.head_sha)
        try:
            await self._app.call(
                "PUT",
                f"{self._repo(token)}/pulls/{pull.number}/update-branch",
                token,
                body=body.model_dump_json(),
            )
        except GitHubRequestError as exc:
            if exc.status in {403, 409, 422}:
                return False
            raise
        return True

    async def close(self, token: InstallationToken, number: int) -> None:
        await self._app.call(
            "PATCH",
            f"{self._repo(token)}/pulls/{number}",
            token,
            body=_CloseBody(state="closed").model_dump_json(),
        )

    async def delete_branch(self, token: InstallationToken, branch: str) -> None:
        try:
            await self._app.call("DELETE", f"{self._repo(token)}/git/refs/heads/{branch}", token)
        except GitHubRequestError as exc:
            if exc.status not in {404, 422}:
                raise


def _parse[T](adapter: TypeAdapter[T], response: httpx.Response) -> T:
    try:
        return adapter.validate_json(response.content)
    except ValidationError:
        raise DependencyUnavailableError("GitHub returned an unexpected body") from None


def _pull(p: _Pull) -> PullInfo:
    base_repo = p.base.repo.full_name if p.base.repo else None
    head_repo = p.head.repo.full_name if p.head.repo else None
    return PullInfo(
        number=p.number,
        title=p.title,
        url=p.html_url,
        body=p.body or "",
        draft=p.draft,
        open=p.state == "open",
        merged=p.merged,
        author=p.user.login,
        head_sha=p.head.sha,
        branch=p.head.ref,
        from_fork=head_repo is None or head_repo != base_repo,
        mergeable_state=p.mergeable_state,
    )
