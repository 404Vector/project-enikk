"""Builds a revert pull request without a checkout (ADR-0023 §5).

The revert of commit X on ``main`` puts every file X changed back to its content in
X's parent, on top of the current head. That is exactly ``git revert`` whenever no
later commit touched those files; when one did, the revert is refused instead of
guessed. It needs only the git data API, so core can do it without an LLM.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError

from enikk_core_domain.errors import DependencyUnavailableError
from enikk_github.app import GitHubRequestError
from enikk_kernel.models import FrozenModel

if TYPE_CHECKING:
    import httpx

    from enikk_github.app import GitHubApp, InstallationToken

BRANCH_PREFIX: Final = "agent/revert-"
MAX_FILES: Final = 300


class _Body(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")


class _Sha(_Body):
    sha: str


class _CommitFile(_Body):
    filename: str
    previous_filename: str | None = None


class _CommitInfo(_Body):
    message: str


class _Commit(_Body):
    sha: str
    commit: _CommitInfo
    parents: tuple[_Sha, ...]
    files: tuple[_CommitFile, ...] = ()


class _GitCommit(_Body):
    sha: str
    tree: _Sha


class _RefObject(_Body):
    sha: str


class _Ref(_Body):
    object: _RefObject


class _TreeItem(_Body):
    path: str
    mode: str
    type: str
    sha: str


class _Tree(_Body):
    sha: str
    tree: tuple[_TreeItem, ...] = ()
    truncated: bool = False


class _PullRef(_Body):
    number: int
    html_url: str


class _NewTreeEntry(FrozenModel):
    path: str
    mode: str
    type: Literal["blob"] = "blob"
    sha: str | None
    """``None`` deletes the path from the base tree."""


class _NewTree(FrozenModel):
    base_tree: str
    tree: tuple[_NewTreeEntry, ...]


class _NewCommit(FrozenModel):
    message: str
    tree: str
    parents: tuple[str, ...]


class _NewRef(FrozenModel):
    ref: str
    sha: str


class _NewPull(FrozenModel):
    title: str
    head: str
    base: str
    body: str


_COMMIT: Final = TypeAdapter(_Commit)
_GIT_COMMIT: Final = TypeAdapter(_GitCommit)
_REF: Final = TypeAdapter(_Ref)
_TREE: Final = TypeAdapter(_Tree)
_SHA: Final = TypeAdapter(_Sha)
_PULL_REF: Final = TypeAdapter(_PullRef)
_PULL_REFS: Final = TypeAdapter(tuple[_PullRef, ...])


class RevertPull(FrozenModel):
    number: int
    url: str
    branch: str
    existing: bool = False
    """The revert PR for this commit was already open."""


class RevertRefused(FrozenModel):
    reason: str
    conflicts: tuple[str, ...] = Field(default=(), max_length=50)


class Reverter:
    def __init__(self, app: GitHubApp) -> None:
        self._app = app

    def _repo(self, token: InstallationToken) -> str:
        return f"/repos/{self._app.owner}/{token.repository}"

    async def open_revert(
        self, token: InstallationToken, sha: str, *, base: str = "main", note: str = ""
    ) -> RevertPull | RevertRefused:
        branch = f"{BRANCH_PREFIX}{sha[:12]}"
        existing = await self._open_pull(token, branch)
        if existing is not None:
            return RevertPull(
                number=existing.number, url=existing.html_url, branch=branch, existing=True
            )
        target = _parse(_COMMIT, await self._get(token, f"/commits/{sha}"))
        if len(target.parents) != 1:
            return RevertRefused(reason="only commits with exactly one parent can be reverted")
        if not target.files or len(target.files) >= MAX_FILES:
            return RevertRefused(reason="the commit's file list is empty or too long to revert")
        head = _parse(_REF, await self._get(token, f"/git/ref/heads/{base}")).object.sha
        trees = {
            name: await self._tree(token, commit)
            for name, commit in (("parent", target.parents[0].sha), ("target", sha), ("head", head))
        }
        paths = sorted(
            {f.filename for f in target.files}
            | {f.previous_filename for f in target.files if f.previous_filename}
        )
        conflicts = tuple(p for p in paths if trees["head"].get(p) != trees["target"].get(p))
        if conflicts:
            return RevertRefused(
                reason="later commits changed the same files", conflicts=conflicts[:50]
            )
        entries = tuple(
            _NewTreeEntry(
                path=p,
                mode=trees["parent"][p].mode if p in trees["parent"] else "100644",
                sha=trees["parent"][p].sha if p in trees["parent"] else None,
            )
            for p in paths
        )
        head_tree = _parse(_GIT_COMMIT, await self._get(token, f"/git/commits/{head}")).tree.sha
        tree = await self._post(token, "/git/trees", _NewTree(base_tree=head_tree, tree=entries))
        title = target.commit.message.splitlines()[0] if target.commit.message else sha[:7]
        commit = await self._post(
            token,
            "/git/commits",
            _NewCommit(
                message=f'Revert "{title}"\n\nThis reverts commit {sha}.',
                tree=_parse(_SHA, tree).sha,
                parents=(head,),
            ),
        )
        await self._post(
            token, "/git/refs", _NewRef(ref=f"refs/heads/{branch}", sha=_parse(_SHA, commit).sha)
        )
        body = f"되돌리는 커밋: {sha}\n\n{note}".strip()
        pull = _parse(
            _PULL_REF,
            await self._post(
                token,
                "/pulls",
                _NewPull(title=f'Revert "{title}"'[:250], head=branch, base=base, body=body),
            ),
        )
        return RevertPull(number=pull.number, url=pull.html_url, branch=branch)

    async def _open_pull(self, token: InstallationToken, branch: str) -> _PullRef | None:
        response = await self._app.call(
            "GET",
            f"{self._repo(token)}/pulls",
            token,
            params={"head": f"{self._app.owner}:{branch}", "state": "open"},
        )
        pulls = _parse(_PULL_REFS, response)
        return pulls[0] if pulls else None

    async def _tree(self, token: InstallationToken, commit: str) -> dict[str, _TreeItem]:
        tree_sha = _parse(_GIT_COMMIT, await self._get(token, f"/git/commits/{commit}")).tree.sha
        response = await self._app.call(
            "GET", f"{self._repo(token)}/git/trees/{tree_sha}", token, params={"recursive": "1"}
        )
        tree = _parse(_TREE, response)
        if tree.truncated:
            raise DependencyUnavailableError("the repository tree is too large to revert by API")
        return {item.path: item for item in tree.tree if item.type == "blob"}

    async def _get(self, token: InstallationToken, path: str) -> httpx.Response:
        return await self._app.call("GET", f"{self._repo(token)}{path}", token)

    async def _post(self, token: InstallationToken, path: str, body: FrozenModel) -> httpx.Response:
        try:
            return await self._app.call(
                "POST", f"{self._repo(token)}{path}", token, body=body.model_dump_json()
            )
        except GitHubRequestError as exc:
            raise DependencyUnavailableError(
                f"GitHub refused {path}: {exc.message or exc.status}"
            ) from exc


def _parse[T](adapter: TypeAdapter[T], response: httpx.Response) -> T:
    try:
        return adapter.validate_json(response.content)
    except ValidationError:
        raise DependencyUnavailableError("GitHub returned an unexpected body") from None
