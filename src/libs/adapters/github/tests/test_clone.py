"""Core's own clone: checking a session's bundle and pushing it (ADR-0037 §6b)."""

from __future__ import annotations

import shutil
import uuid
from pathlib import Path

import pytest
from pydantic import SecretStr

import enikk_github.clone as clone_module
from enikk_core_domain.errors import InvalidRequestError
from enikk_github.clone import Author, CloneConfig, CoreClone, with_co_author
from enikk_kernel.errors import ConfigurationError
from enikk_kernel.process import SubprocessRunner
from enikk_kernel.testing import RecordingRunner

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="needs git")

SESSION = uuid.UUID(int=7)
REF = f"refs/heads/session/{SESSION}"
BRANCH = f"agent/{SESSION}"
BOT = Author(name="enikk-deployer[bot]", email="1+enikk-deployer[bot]@users.noreply.github.com")
CO_AUTHOR = "Claude <noreply@anthropic.com>"
TOKEN = SecretStr("ghs_" + "x" * 36)


class Repo:
    def __init__(self, path: Path, *args: str) -> None:
        self.path = path
        self._run = SubprocessRunner()
        path.mkdir(parents=True, exist_ok=True)
        self.git("init", "--quiet", "--initial-branch=main", *args)

    def git(self, *args: str) -> str:
        identity = ["-c", "user.name=someone", "-c", "user.email=someone@example.com"]
        return self._run.run(["git", "-C", str(self.path), *identity, *args]).stdout.strip()

    def commit(self, name: str, text: str, message: str | None = None) -> str:
        (self.path / name).parent.mkdir(parents=True, exist_ok=True)
        (self.path / name).write_text(text)
        self.git("add", name)
        self.git("commit", "--quiet", "-m", message or f"add {name}")
        return self.git("rev-parse", "HEAD")


class World:
    """GitHub (``origin``), a session's worktree (``work``) and core's clone."""

    def __init__(self, tmp: Path) -> None:
        self.tmp = tmp
        self.origin = Repo(tmp / "origin")
        self.base = self.origin.commit("README.md", "hello\n")
        self.work = Repo(tmp / "work")
        self.work.git("fetch", "--quiet", str(self.origin.path), "main")
        self.work.git("checkout", "--quiet", "-b", f"session/{SESSION}", "FETCH_HEAD")
        self.clone = CoreClone(
            CloneConfig(root=tmp / "core", remote_url=f"file://{self.origin.path}"),
            SubprocessRunner(),
        )

    def bundle(self, ref: str = REF, base: str | None = None) -> tuple[bytes, str]:
        path = self.tmp / "s.bundle"
        path.unlink(missing_ok=True)
        self.work.git("bundle", "create", "--quiet", str(path), ref, f"^{base or self.base}")
        return path.read_bytes(), self.work.git("rev-parse", ref)

    def push(self, data: bytes, head: str, **changes: object) -> clone_module.Pushed:
        args: dict[str, object] = {
            "session_ref": REF,
            "base": self.base,
            "head": head,
            "branch": BRANCH,
            "token": TOKEN,
            "author": BOT,
            "co_author": CO_AUTHOR,
            **changes,
        }
        return self.clone.push(data, **args)  # type: ignore[arg-type]


@pytest.fixture
def world(tmp_path: Path) -> World:
    return World(tmp_path)


def test_commits_are_pushed_with_the_bot_as_author_and_a_co_author(world: World) -> None:
    original = world.work.commit("docs/a.md", "a\n", "docs: add a")
    data, head = world.bundle()
    pushed = world.push(data, head)

    assert pushed.commits == 1
    assert pushed.files == ("docs/a.md",)
    assert world.origin.git("rev-parse", BRANCH) == pushed.head
    shown = world.origin.git("show", "-s", "--format=%an|%ae|%cn|%ce|%B", pushed.head)
    assert shown.startswith(f"{BOT.name}|{BOT.email}|{BOT.name}|{BOT.email}|docs: add a")
    assert f"Co-Authored-By: {CO_AUTHOR}" in shown
    tree = "%T"
    assert world.origin.git("show", "-s", f"--format={tree}", pushed.head) == world.work.git(
        "show", "-s", f"--format={tree}", original
    )
    date = "%aI"
    assert world.origin.git("show", "-s", f"--format={date}", pushed.head) == world.work.git(
        "show", "-s", f"--format={date}", original
    )


def test_a_second_submission_fast_forwards_the_same_branch(world: World) -> None:
    world.work.commit("docs/a.md", "a\n")
    first = world.push(*world.bundle())
    world.work.commit("docs/b.md", "b\n")
    second = world.push(*world.bundle())

    assert second.commits == 2
    assert world.origin.git("rev-parse", f"{second.head}~1") == first.head
    assert world.origin.git("rev-parse", BRANCH) == second.head


def test_a_diverged_branch_is_not_force_pushed(world: World) -> None:
    world.work.commit("docs/a.md", "a\n")
    first = world.push(*world.bundle())
    world.work.git("commit", "--quiet", "--amend", "-m", "rewritten")
    with pytest.raises(InvalidRequestError, match="never force-pushes"):
        world.push(*world.bundle())
    assert world.origin.git("rev-parse", BRANCH) == first.head


def test_the_bundle_must_hold_exactly_this_sessions_branch(world: World) -> None:
    world.work.git("checkout", "--quiet", "-b", "session/other")
    world.work.commit("docs/a.md", "a\n")
    data, head = world.bundle("refs/heads/session/other")
    with pytest.raises(InvalidRequestError, match="must hold exactly"):
        world.push(data, head)
    with pytest.raises(InvalidRequestError, match="must hold exactly"):
        world.push(*world.bundle("refs/heads/session/other"), session_ref=REF)


def test_main_and_other_branches_are_never_pushed(world: World) -> None:
    world.work.commit("docs/a.md", "a\n")
    data, head = world.bundle()
    for branch in ("main", "release"):
        with pytest.raises(InvalidRequestError, match="only agent branches"):
            world.push(data, head, branch=branch)
    assert world.origin.git("rev-parse", "main") == world.base


def test_the_base_must_be_on_main(world: World) -> None:
    side = world.work.commit("docs/a.md", "a\n")
    world.work.commit("docs/b.md", "b\n")
    data, head = world.bundle(base=side)
    with pytest.raises(InvalidRequestError, match="not a commit on main"):
        world.push(data, head, base=side)


def test_merge_commits_are_refused(world: World) -> None:
    world.work.git("checkout", "--quiet", "-b", "side")
    world.work.commit("docs/a.md", "a\n")
    world.work.git("checkout", "--quiet", f"session/{SESSION}")
    world.work.commit("docs/b.md", "b\n")
    world.work.git("merge", "--quiet", "--no-ff", "-m", "merge side", "side")
    with pytest.raises(InvalidRequestError, match="merge commit"):
        world.push(*world.bundle())


def test_workflow_changes_are_refused(world: World) -> None:
    world.work.commit(".github/workflows/ci.yaml", "on: push\n")
    with pytest.raises(InvalidRequestError, match="workflow files"):
        world.push(*world.bundle())


def test_commit_and_file_limits(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    world.work.commit("docs/a.md", "a\n")
    world.work.commit("docs/b.md", "b\n")
    monkeypatch.setattr(clone_module, "MAX_FILES", 1)
    with pytest.raises(InvalidRequestError, match="at most 1"):
        world.push(*world.bundle())
    monkeypatch.setattr(clone_module, "MAX_COMMITS", 1)
    with pytest.raises(InvalidRequestError, match="at most 1"):
        world.push(*world.bundle())


def test_an_unreadable_bundle_is_refused(world: World) -> None:
    with pytest.raises(InvalidRequestError, match="cannot be read"):
        world.push(b"not a bundle", world.base)


def test_core_never_runs_git_in_the_shared_volume(tmp_path: Path) -> None:
    for root in (Path("/data"), Path("/data/repo.git")):
        with pytest.raises(ConfigurationError, match="must not be in"):
            CoreClone(
                CloneConfig(root=root, remote_url="https://github.com/o/r.git"), RecordingRunner()
            )

    runner = RecordingRunner()
    root = tmp_path / "core"
    clone = CoreClone(CloneConfig(root=root, remote_url="https://github.com/o/r.git"), runner)
    with pytest.raises(InvalidRequestError):
        clone.push(
            b"x",
            session_ref=REF,
            base="a" * 40,
            head="b" * 40,
            branch=BRANCH,
            token=TOKEN,
            author=BOT,
            co_author=CO_AUTHOR,
        )
    assert runner.calls
    for call in runner.calls:
        assert call.cwd == root
        assert "/data" not in " ".join(call.argv)
        assert "core.hooksPath=/dev/null" in call.argv
        assert TOKEN.get_secret_value() not in " ".join(call.argv)
    fetch = next(c for c in runner.calls if "fetch" in c.argv)
    assert "GIT_CONFIG_VALUE_0" in fetch.env_keys
    assert not list((root / "incoming").iterdir())


def test_co_author_is_added_once() -> None:
    assert with_co_author("fix: x", CO_AUTHOR) == f"fix: x\n\nCo-Authored-By: {CO_AUTHOR}\n"
    kept = "fix: x\n\nco-authored-by: Someone <a@b.c>"
    assert with_co_author(kept, CO_AUTHOR) == kept + "\n"
