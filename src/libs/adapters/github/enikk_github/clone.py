"""Core's own clone: checks a session's bundle and pushes it (ADR-0037 §6b, 2b).

The bundle is data from an untrusted session. It is written to core's own directory
and read with ``git bundle``; core never runs git in the shared volume, so nothing a
session planted there (hooks, config) can run with core's token. Every git command
ignores the system and global config, disables hooks and fsmonitor, and runs under a
time limit. The token reaches git only through the environment, never argv.

Checks: the bundle holds exactly the session's branch at ``head``; ``base`` is on
main; no merge commits; at most ``MAX_COMMITS`` commits and ``MAX_FILES`` files; no
change under ``.github/workflows/``. The commits are written again with the App's bot
as author and committer (tree, message and dates kept) and pushed to the agent branch
without force: an existing branch must be an ancestor of the new head.
"""

from __future__ import annotations

import base64
import contextlib
import shutil
import threading
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Final

from pydantic import Field, StringConstraints

from enikk_contracts.pulls import MAX_COMMITS, MAX_FILES
from enikk_core_domain.errors import DependencyUnavailableError, InvalidRequestError
from enikk_kernel.errors import ConfigurationError
from enikk_kernel.models import FrozenModel

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping

    from pydantic import SecretStr

    from enikk_kernel.process import ProcessResult, ProcessRunner

WORKFLOWS: Final = ".github/workflows/"
PROTECTED_BRANCHES: Final = frozenset({"main", "master"})
CO_AUTHOR_KEY: Final = "co-authored-by:"
_SAFE_ENV: Final = {
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_TERMINAL_PROMPT": "0",
}


class CloneConfig(FrozenModel):
    root: Path
    """Core's own directory (an emptyDir). Never the shared volume."""
    remote_url: Annotated[str, StringConstraints(pattern=r"^(https|file)://[^\s\"\\]+$")]
    branch: Annotated[str, StringConstraints(pattern=r"^[\w./-]+$")] = "main"
    git_timeout_seconds: int = Field(default=120, ge=1)
    forbidden: tuple[Path, ...] = (Path("/data"),)
    """Paths core must never run git in (the runtime's shared volume)."""

    @property
    def repo_dir(self) -> Path:
        return self.root / "repo.git"


class Author(FrozenModel):
    name: str
    email: str


class Pushed(FrozenModel):
    head: str
    commits: int
    files: tuple[str, ...]


class CoreClone:
    """Blocking; call it in a thread. One submission at a time."""

    def __init__(self, config: CloneConfig, runner: ProcessRunner) -> None:
        root = config.root.resolve()
        for forbidden in config.forbidden:
            if root == forbidden or root.is_relative_to(forbidden):
                raise ConfigurationError(f"core's clone must not be in {forbidden}: {root}")
        self._config = config
        self._runner = runner
        self._lock = threading.Lock()

    def push(
        self,
        bundle: bytes,
        *,
        session_ref: str,
        base: str,
        head: str,
        branch: str,
        token: SecretStr,
        author: Author,
        co_author: str,
    ) -> Pushed:
        if branch in PROTECTED_BRANCHES or not branch.startswith("agent/"):
            raise InvalidRequestError(f"core pushes only agent branches, not {branch}")
        auth = _auth_env(token)
        with self._lock:
            self._init()
            self._fetch(auth, f"+refs/heads/{self._config.branch}:refs/remotes/origin/main")
            if not self._is_ancestor(base, "refs/remotes/origin/main"):
                raise InvalidRequestError(f"base {base} is not a commit on main")
            with self._incoming(bundle) as path:
                self._unbundle(path, session_ref, head)
            commits = self._commits(base, head)
            files = self._files(base, head)
            new_head = self._rewrite(commits, base, author, co_author)
            remote = self._remote_head(auth, branch)
            if remote is not None:
                self._fetch(auth, f"+refs/heads/{branch}:refs/remotes/origin/{branch}")
                if not self._is_ancestor(remote, new_head):
                    raise InvalidRequestError(
                        f"{branch} on GitHub has commits this submission does not build on; "
                        "core never force-pushes. Rebuild the branch on top of them."
                    )
            if remote != new_head:
                self._git(
                    "push",
                    "--quiet",
                    self._config.remote_url,
                    f"{new_head}:refs/heads/{branch}",
                    env=auth,
                )
        return Pushed(head=new_head, commits=len(commits), files=files)

    def subject(self, commit: str) -> str:
        """The first line of ``commit``'s message in core's clone."""
        with self._lock:
            return self._git("show", "-s", "--format=%s", commit).strip() or "Update"

    # ------------------------------------------------------------ steps

    def _init(self) -> None:
        repo = self._config.repo_dir
        (self._config.root / "tmp").mkdir(parents=True, exist_ok=True)
        if not (repo / "HEAD").is_file():
            shutil.rmtree(repo, ignore_errors=True)
            self._git("init", "--quiet", "--bare", "--template=", str(repo), git_dir=False)

    def _fetch(self, auth: Mapping[str, str], refspec: str) -> None:
        self._git("fetch", "--quiet", "--no-tags", self._config.remote_url, refspec, env=auth)

    def _remote_head(self, auth: Mapping[str, str], branch: str) -> str | None:
        out = self._git("ls-remote", self._config.remote_url, f"refs/heads/{branch}", env=auth)
        for line in out.splitlines():
            sha, _, ref = line.partition("\t")
            if ref == f"refs/heads/{branch}":
                return sha
        return None

    @contextlib.contextmanager
    def _incoming(self, bundle: bytes) -> Iterator[Path]:
        incoming = self._config.root / "incoming"
        incoming.mkdir(parents=True, exist_ok=True)
        path = incoming / f"{uuid.uuid4()}.bundle"
        path.write_bytes(bundle)
        try:
            yield path
        finally:
            path.unlink(missing_ok=True)

    def _unbundle(self, path: Path, session_ref: str, head: str) -> None:
        listed = self._raw(["bundle", "list-heads", str(path)])
        if not listed.ok:
            raise InvalidRequestError("the bundle cannot be read")
        heads = [tuple(line.split(" ", 1)) for line in listed.stdout.splitlines() if line.strip()]
        if heads != [(head, session_ref)]:
            shown = ", ".join(" ".join(h) for h in heads[:5]) or "none"
            raise InvalidRequestError(
                f"the bundle must hold exactly {session_ref} at {head}; it holds {shown}"
            )
        if not self._raw(["bundle", "verify", "--quiet", str(path)]).ok:
            raise InvalidRequestError("the bundle needs commits that are not on main")
        self._git("bundle", "unbundle", str(path))
        self._git("update-ref", "refs/submissions/head", head)

    def _commits(self, base: str, head: str) -> list[str]:
        if not self._is_ancestor(base, head):
            raise InvalidRequestError(f"{head} does not build on base {base}")
        out = self._git("rev-list", "--reverse", "--parents", f"{base}..{head}")
        commits: list[str] = []
        for line in out.splitlines():
            shas = line.split()
            if len(shas) != 2:
                raise InvalidRequestError(f"commit {shas[0]} is a merge commit; rebase instead")
            commits.append(shas[0])
        if not commits:
            raise InvalidRequestError("there are no commits after main to submit")
        if len(commits) > MAX_COMMITS:
            raise InvalidRequestError(f"{len(commits)} commits; at most {MAX_COMMITS}")
        return commits

    def _files(self, base: str, head: str) -> tuple[str, ...]:
        out = self._git("log", "--no-renames", "--name-only", "--format=", f"{base}..{head}")
        files = tuple(sorted({line for line in out.splitlines() if line.strip()}))
        if len(files) > MAX_FILES:
            raise InvalidRequestError(f"{len(files)} files changed; at most {MAX_FILES}")
        workflows = [f for f in files if f.startswith(WORKFLOWS)]
        if workflows:
            raise InvalidRequestError(
                f"workflow files may not change here: {', '.join(workflows[:5])}"
            )
        return files

    def _rewrite(self, commits: list[str], base: str, author: Author, co_author: str) -> str:
        parent = base
        for commit in commits:
            fields = self._git("show", "-s", "--format=%T%n%aI%n%cI", commit).split("\n")
            tree, authored, committed = fields[0], fields[1], fields[2]
            message = self._git("show", "-s", "--format=%B", commit).strip()
            env = {
                "GIT_AUTHOR_NAME": author.name,
                "GIT_AUTHOR_EMAIL": author.email,
                "GIT_AUTHOR_DATE": authored,
                "GIT_COMMITTER_NAME": author.name,
                "GIT_COMMITTER_EMAIL": author.email,
                "GIT_COMMITTER_DATE": committed,
            }
            text = with_co_author(message, co_author)
            parent = self._git(
                "commit-tree", tree, "-p", parent, "-F", "-", stdin=text, env=env
            ).strip()
        return parent

    def _is_ancestor(self, ancestor: str, descendant: str) -> bool:
        result = self._raw(["merge-base", "--is-ancestor", ancestor, descendant])
        if result.returncode in (0, 1):
            return result.returncode == 0
        return False

    # ------------------------------------------------------------ git

    def _git(
        self,
        *args: str,
        git_dir: bool = True,
        stdin: str | None = None,
        env: Mapping[str, str] | None = None,
    ) -> str:
        result = self._raw(list(args), git_dir=git_dir, stdin=stdin, env=env)
        if result.ok:
            return result.stdout
        detail = result.stderr.strip().splitlines()[-1:] or [""]
        raise DependencyUnavailableError(
            f"git {args[0]} failed ({result.returncode}): {detail[0][:300]}"
        )

    def _raw(
        self,
        args: list[str],
        *,
        git_dir: bool = True,
        stdin: str | None = None,
        env: Mapping[str, str] | None = None,
    ) -> ProcessResult:
        c = self._config
        argv = [
            "timeout",
            "--kill-after=5",
            str(c.git_timeout_seconds),
            "git",
            *([f"--git-dir={c.repo_dir}"] if git_dir else []),
            "-c",
            "core.hooksPath=/dev/null",
            "-c",
            "core.fsmonitor=false",
            "-c",
            "protocol.ext.allow=never",
            *args,
        ]
        merged = {**_SAFE_ENV, "TMPDIR": str(c.root / "tmp"), **(env or {})}
        return self._runner.run(argv, cwd=c.root, check=False, stdin=stdin, env=merged)


def with_co_author(message: str, co_author: str) -> str:
    """``message`` with a ``Co-Authored-By`` trailer, unless it already has one."""
    text = message.strip() or "Update"
    if any(line.strip().lower().startswith(CO_AUTHOR_KEY) for line in text.splitlines()):
        return text + "\n"
    return f"{text}\n\nCo-Authored-By: {co_author}\n"


def _auth_env(token: SecretStr) -> dict[str, str]:
    """git config through the environment: the token never appears in argv."""
    basic = base64.b64encode(f"x-access-token:{token.get_secret_value()}".encode()).decode()
    return {
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "http.https://github.com/.extraheader",
        "GIT_CONFIG_VALUE_0": f"AUTHORIZATION: basic {basic}",
    }
