"""The task's git workspace: credentials for git and gh, the clone and the branch.

The development token is written only to gh's own config file, which git reads
through ``gh auth git-credential``. It is never put in the environment or argv
(ADR-0021). The file is deleted when the task ends.
"""

from __future__ import annotations

import logging
import os
import shutil
from pathlib import Path
from typing import TYPE_CHECKING, Final

import yaml
from pydantic import Field

from enikk_kernel.errors import EnikkError
from enikk_kernel.models import FrozenModel

if TYPE_CHECKING:
    from enikk_contracts.github import GitHubToken
    from enikk_kernel.process import ProcessRunner

_log = logging.getLogger(__name__)

GITHUB_HOST: Final = "github.com"
REPO_DIR: Final = "repo"


class WorkspaceError(EnikkError):
    """The workspace could not be prepared."""


class _GhHost(FrozenModel):
    oauth_token: str
    user: str
    git_protocol: str = "https"


class _GhHosts(FrozenModel):
    hosts: dict[str, _GhHost] = Field(default_factory=dict)


class Workspace(FrozenModel):
    """Where things are, once prepared."""

    repo_dir: Path
    branch: str
    tool_env: dict[str, str]
    continuing: bool = False
    """The branch already existed: it carries an open PR from earlier work (ADR-0025)."""


class GitWorkspace:
    def __init__(self, runner: ProcessRunner, root: Path, tool_home: Path) -> None:
        self._runner = runner
        self._root = root
        self._tools = tool_home

    @property
    def tool_env(self) -> dict[str, str]:
        """Paths only. Also set on this process so git and gh find them."""
        return {
            "GIT_CONFIG_GLOBAL": str(self._tools / "gitconfig"),
            "GH_CONFIG_DIR": str(self._tools / "gh"),
            "GIT_TERMINAL_PROMPT": "0",
            "GH_PROMPT_DISABLED": "1",
            "GH_NO_UPDATE_NOTIFIER": "1",
            "UV_CACHE_DIR": str(self._tools / "uv-cache"),
            "XDG_CACHE_HOME": str(self._tools / "cache"),
        }

    def prepare(self, token: GitHubToken, branch: str) -> Workspace:
        """Clone and check out ``branch``: the existing one if it is on GitHub, else a
        new one from main. One branch per thread, so later requests in the same thread
        add commits to the same pull request (ADR-0025)."""
        env = self.tool_env
        os.environ.update(env)
        self._write_credentials(token)
        repo_dir = self._root / REPO_DIR
        url = f"https://{GITHUB_HOST}/{token.owner}/{token.repository}.git"
        self._git("clone", "--depth", "100", url, str(repo_dir))
        found = self._runner.run(
            ["git", "ls-remote", "--heads", "origin", branch], cwd=repo_dir, check=False
        )
        if not found.ok:
            raise WorkspaceError(f"git ls-remote failed: {found.stderr.strip()[:500]}")
        continuing = bool(found.stdout.strip())
        if continuing:
            remote = f"refs/remotes/origin/{branch}"
            self._git(
                "fetch", "--depth", "100", "origin", f"+refs/heads/{branch}:{remote}", cwd=repo_dir
            )
            self._git("switch", "--create", branch, "--track", f"origin/{branch}", cwd=repo_dir)
        else:
            self._git("switch", "--create", branch, cwd=repo_dir)
        _log.info(
            "cloned %s/%s on %s branch %s",
            token.owner,
            token.repository,
            "existing" if continuing else "new",
            branch,
        )
        return Workspace(repo_dir=repo_dir, branch=branch, tool_env=env, continuing=continuing)

    def revoke_and_forget(self) -> None:
        """Best effort: revoke the token, then delete every credential file."""
        if (self._tools / "gh" / "hosts.yml").exists():
            result = self._runner.run(
                ["gh", "api", "--method", "DELETE", "/installation/token"], check=False
            )
            if not result.ok:
                _log.warning("could not revoke the development token (it expires in 1h)")
        shutil.rmtree(self._tools, ignore_errors=True)

    def _write_credentials(self, token: GitHubToken) -> None:
        gh_dir = self._tools / "gh"
        gh_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        hosts = _GhHosts(
            hosts={
                GITHUB_HOST: _GhHost(
                    oauth_token=token.token.get_secret_value(), user=token.author_name
                )
            }
        )
        hosts_file = gh_dir / "hosts.yml"
        hosts_file.touch(mode=0o600)
        hosts_file.write_text(yaml.safe_dump(hosts.model_dump()["hosts"]), encoding="utf-8")
        gitconfig = self._tools / "gitconfig"
        gitconfig.write_text(
            "[user]\n"
            f"\tname = {token.author_name}\n"
            f"\temail = {token.author_email}\n"
            f'[credential "https://{GITHUB_HOST}"]\n'
            "\thelper =\n"
            "\thelper = !gh auth git-credential\n"
            "[init]\n"
            "\tdefaultBranch = main\n",
            encoding="utf-8",
        )

    def _git(self, *args: str, cwd: Path | None = None) -> None:
        result = self._runner.run(["git", *args], cwd=cwd, check=False)
        if not result.ok:
            raise WorkspaceError(f"git {args[0]} failed: {result.stderr.strip()[:500]}")
