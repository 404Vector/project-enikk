from __future__ import annotations

import os
import shutil
import uuid
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest

from enikk_kernel.process import SubprocessRunner
from enikk_kernel.testing import ManualClock
from enikk_runtime.memory import HEADER as MEMORY_HEADER
from enikk_runtime.memory import with_memories
from enikk_runtime.workspace import (
    GIT_USER_EMAIL,
    GIT_USER_NAME,
    HEADER,
    SessionWorkspaces,
    WorkspaceConfig,
    with_workspace,
)

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="needs git")

SESSION = uuid.UUID(int=1)
OTHER = uuid.UUID(int=2)


class Origin:
    """A local repository standing in for GitHub."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.mkdir(parents=True, exist_ok=True)
        self._run = SubprocessRunner()
        self.git("init", "--quiet", "--initial-branch=main")

    def git(self, *args: str) -> str:
        identity = ["-c", "user.name=test", "-c", "user.email=test@example.com"]
        return self._run.run(["git", "-C", str(self.path), *identity, *args]).stdout.strip()

    def commit(self, name: str, text: str) -> str:
        (self.path / name).parent.mkdir(parents=True, exist_ok=True)
        (self.path / name).write_text(text)
        self.git("add", name)
        self.git("commit", "--quiet", "-m", f"add {name}")
        return self.git("rev-parse", "HEAD")


@pytest.fixture
def origin(tmp_path: Path) -> Origin:
    return Origin(tmp_path / "origin")


def _workspaces(
    tmp_path: Path, origin: Origin, clock: ManualClock, **overrides: object
) -> SessionWorkspaces:
    config = WorkspaceConfig.model_validate(
        {
            "root": tmp_path / "data",
            "repository": "404Vector/project-enikk",
            "remote_url": f"file://{origin.path}",
            "min_free_bytes": 0,
            **overrides,
        }
    )
    return SessionWorkspaces(config, SubprocessRunner(), clock)


def _clock() -> ManualClock:
    return ManualClock(datetime.now(UTC))


def test_main_is_fetched_and_its_files_are_readable_at_the_commit(
    tmp_path: Path, origin: Origin
) -> None:
    commit = origin.commit("docs/README.md", "hello\n")
    ws = _workspaces(tmp_path, origin, _clock()).prepare(SESSION)

    assert ws.commit == commit
    assert ws.fresh
    assert ws.snapshot_dir == tmp_path / "data" / "snapshots" / commit
    assert (ws.snapshot_dir / "docs" / "README.md").read_text() == "hello\n"
    assert ws.read_paths == (ws.snapshot_dir,)


def test_the_session_gets_its_own_worktree_and_tmp(tmp_path: Path, origin: Origin) -> None:
    origin.commit("a.txt", "a\n")
    ws = _workspaces(tmp_path, origin, _clock()).prepare(SESSION)

    data = tmp_path / "data"
    assert ws.worktree_dir == data / "worktrees" / str(SESSION)
    assert (ws.worktree_dir / "a.txt").read_text() == "a\n"
    assert ws.branch == f"session/{SESSION}"
    assert ws.tmp_dir == data / "tmp" / str(SESSION)
    assert ws.tmp_dir.is_dir()
    assert ws.write_paths == (data / "repo.git", ws.worktree_dir, ws.tmp_dir)


def test_the_worktree_is_reused_by_the_same_session(tmp_path: Path, origin: Origin) -> None:
    origin.commit("a.txt", "a\n")
    clock = _clock()
    workspaces = _workspaces(tmp_path, origin, clock)
    first = workspaces.prepare(SESSION)
    assert first.worktree_dir is not None
    (first.worktree_dir / "notes.md").write_text("work in progress\n")

    origin.commit("b.txt", "b\n")
    clock.advance(timedelta(minutes=5))
    second = workspaces.prepare(SESSION)

    assert second.worktree_dir == first.worktree_dir
    assert (second.worktree_dir / "notes.md").read_text() == "work in progress\n"
    assert second.commit != first.commit
    assert second.snapshot_dir is not None
    assert (second.snapshot_dir / "b.txt").is_file()


def test_fetch_waits_for_the_interval(tmp_path: Path, origin: Origin) -> None:
    first_commit = origin.commit("a.txt", "a\n")
    clock = _clock()
    workspaces = _workspaces(tmp_path, origin, clock)
    workspaces.prepare(SESSION)

    origin.commit("b.txt", "b\n")
    clock.advance(timedelta(seconds=10))
    assert workspaces.prepare(SESSION).commit == first_commit


def test_a_failed_fetch_keeps_the_last_snapshot(tmp_path: Path, origin: Origin) -> None:
    commit = origin.commit("a.txt", "a\n")
    clock = _clock()
    _workspaces(tmp_path, origin, clock).prepare(SESSION)

    clock.advance(timedelta(minutes=5))
    broken = _workspaces(tmp_path, origin, clock, remote_url=f"file://{tmp_path}/missing")
    ws = broken.prepare(SESSION)

    assert not ws.fresh
    assert ws.commit == commit
    assert "이전 커밋일 수 있다" in with_workspace("안녕", ws)


def test_nothing_fetched_yet_still_gives_a_tmp_dir(tmp_path: Path, origin: Origin) -> None:
    ws = _workspaces(tmp_path, origin, _clock()).prepare(SESSION)
    assert ws.commit is None
    assert ws.snapshot_dir is None
    assert ws.worktree_dir is None
    assert ws.write_paths == (ws.tmp_dir,)


def test_planted_config_and_hooks_are_reset(tmp_path: Path, origin: Origin) -> None:
    origin.commit("a.txt", "a\n")
    clock = _clock()
    workspaces = _workspaces(tmp_path, origin, clock)
    workspaces.prepare(SESSION)

    repo = tmp_path / "data" / "repo.git"
    marker = tmp_path / "pwned"
    with (repo / "config").open("a") as config:
        config.write(f"[core]\n\tfsmonitor = touch {marker}\n")
    (repo / "hooks").mkdir()
    hook = repo / "hooks" / "reference-transaction"
    hook.write_text(f"#!/bin/sh\ntouch {marker}\n")
    hook.chmod(0o755)

    origin.commit("b.txt", "b\n")
    clock.advance(timedelta(minutes=5))
    workspaces.prepare(OTHER)

    assert not marker.exists()
    assert "fsmonitor = false" in (repo / "config").read_text()
    assert "touch" not in (repo / "config").read_text()
    assert not (repo / "hooks").exists()


def test_a_worktree_can_commit_without_a_global_identity(
    tmp_path: Path, origin: Origin, monkeypatch: pytest.MonkeyPatch
) -> None:
    origin.commit("a.txt", "a\n")
    worktree = _workspaces(tmp_path, origin, _clock()).prepare(SESSION).worktree_dir
    assert worktree is not None
    config = (tmp_path / "data" / "repo.git" / "config").read_text()
    assert f"name = {GIT_USER_NAME}" in config
    assert f"email = {GIT_USER_EMAIL}" in config

    for name in ("AUTHOR", "COMMITTER"):
        for part in ("NAME", "EMAIL"):
            monkeypatch.delenv(f"GIT_{name}_{part}", raising=False)
    monkeypatch.delenv("EMAIL", raising=False)
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    (worktree / "b.txt").write_text("b\n")
    run = SubprocessRunner()
    run.run(["git", "-C", str(worktree), "add", "b.txt"])
    run.run(["git", "-C", str(worktree), "commit", "--quiet", "-m", "add b"])
    author = run.run(["git", "-C", str(worktree), "log", "-1", "--format=%an <%ae>"])
    assert author.stdout.strip() == f"{GIT_USER_NAME} <{GIT_USER_EMAIL}>"


def test_gc_is_off(tmp_path: Path, origin: Origin) -> None:
    origin.commit("a.txt", "a\n")
    _workspaces(tmp_path, origin, _clock()).prepare(SESSION)
    config = (tmp_path / "data" / "repo.git" / "config").read_text()
    assert "auto = 0" in config


def test_stale_locks_are_cleared_and_fresh_ones_kept(tmp_path: Path, origin: Origin) -> None:
    origin.commit("a.txt", "a\n")
    clock = _clock()
    workspaces = _workspaces(tmp_path, origin, clock)
    workspaces.prepare(SESSION)

    repo = tmp_path / "data" / "repo.git"
    stale = repo / "worktrees" / str(SESSION) / "index.lock"
    fresh = repo / "packed-refs.lock"
    stale.write_text("")
    fresh.write_text("")
    old = (clock.now() - timedelta(hours=1)).timestamp()
    os.utime(stale, (old, old))

    workspaces.prepare(SESSION)
    assert not stale.exists()
    assert fresh.exists()


def test_idle_workspaces_are_removed_after_the_idle_days(tmp_path: Path, origin: Origin) -> None:
    origin.commit("a.txt", "a\n")
    clock = _clock()
    workspaces = _workspaces(tmp_path, origin, clock)
    idle = workspaces.prepare(OTHER)
    assert idle.worktree_dir is not None

    clock.advance(timedelta(days=6))
    workspaces.prepare(SESSION)
    assert idle.worktree_dir.is_dir()

    clock.advance(timedelta(days=2))
    active = workspaces.prepare(SESSION)
    assert not idle.worktree_dir.exists()
    assert not idle.tmp_dir.exists()
    assert active.worktree_dir is not None
    assert active.worktree_dir.is_dir()
    branches = SubprocessRunner().run(
        ["git", f"--git-dir={tmp_path / 'data' / 'repo.git'}", "branch", "--list"]
    )
    assert f"session/{OTHER}" not in branches.stdout


def test_a_worktree_deleted_by_hand_is_made_again(tmp_path: Path, origin: Origin) -> None:
    origin.commit("a.txt", "a\n")
    workspaces = _workspaces(tmp_path, origin, _clock())
    first = workspaces.prepare(SESSION)
    assert first.worktree_dir is not None
    shutil.rmtree(first.worktree_dir)

    second = workspaces.prepare(SESSION)
    assert second.worktree_dir is not None
    assert (second.worktree_dir / "a.txt").is_file()


def test_no_new_worktree_when_the_volume_is_nearly_full(tmp_path: Path, origin: Origin) -> None:
    origin.commit("a.txt", "a\n")
    full = _workspaces(tmp_path, origin, _clock(), min_free_bytes=2**62)
    ws = full.prepare(SESSION)
    assert ws.worktree_dir is None
    assert ws.snapshot_dir is not None


def test_old_snapshots_are_dropped(tmp_path: Path, origin: Origin) -> None:
    clock = _clock()
    workspaces = _workspaces(tmp_path, origin, clock, keep_snapshots=2)
    commits = []
    for name in ("a", "b", "c"):
        commits.append(origin.commit(f"{name}.txt", name))
        clock.advance(timedelta(minutes=5))
        workspaces.prepare(SESSION)
    snapshots = {p.name for p in (tmp_path / "data" / "snapshots").iterdir()}
    assert snapshots == set(commits[1:])


def test_git_commands_go_through_the_wrapper(tmp_path: Path, origin: Origin) -> None:
    origin.commit("a.txt", "a\n")
    wrapped: list[list[str]] = []

    def wrap(argv: Sequence[str]) -> list[str]:
        wrapped.append(list(argv))
        return list(argv)

    config = WorkspaceConfig(
        root=tmp_path / "data",
        repository="404Vector/project-enikk",
        remote_url=f"file://{origin.path}",
        min_free_bytes=0,
    )
    SessionWorkspaces(config, SubprocessRunner(), _clock(), wrap=wrap).prepare(SESSION)

    assert wrapped
    assert all(argv[0] == "timeout" for argv in wrapped)
    git_calls = [argv for argv in wrapped if "--git-dir=" in " ".join(argv)]
    assert git_calls
    for argv in git_calls:
        assert "GIT_CONFIG_NOSYSTEM=1" in argv
        assert "GIT_CONFIG_GLOBAL=/dev/null" in argv
        assert "core.hooksPath=/dev/null" in argv


def test_remote_url_must_not_inject_config(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="remote_url"):
        WorkspaceConfig(
            root=tmp_path,
            repository="a/b",
            remote_url='https://example.com/x.git"\n[core]\n\tfsmonitor = sh',
        )


def test_the_note_names_the_commit_and_paths(tmp_path: Path, origin: Origin) -> None:
    commit = origin.commit("a.txt", "a\n")
    ws = _workspaces(tmp_path, origin, _clock()).prepare(SESSION)
    text = with_workspace("리뷰해 줘", ws)
    assert text.startswith(HEADER)
    assert commit in text
    assert str(ws.snapshot_dir) in text
    assert str(ws.worktree_dir) in text
    assert str(ws.tmp_dir) in text
    assert text.endswith("[요청]\n리뷰해 줘")


def test_the_note_goes_before_the_memories_with_one_request_marker(
    tmp_path: Path, origin: Origin
) -> None:
    from enikk_contracts.memory import MemoryNote, NoteKind

    origin.commit("a.txt", "a\n")
    ws = _workspaces(tmp_path, origin, _clock()).prepare(SESSION)
    note = MemoryNote(path="vault/x.md", title="x", kind=NoteKind.FACT, text="y")
    text = with_workspace(with_memories("안녕", [note]), ws)
    assert text.startswith(HEADER)
    assert MEMORY_HEADER in text
    assert text.count("[요청]") == 1


def test_no_workspace_leaves_the_input_alone() -> None:
    assert with_workspace("안녕", None) == "안녕"


def test_a_moved_ref_does_not_change_the_snapshot(tmp_path: Path, origin: Origin) -> None:
    commit = origin.commit("a.txt", "a\n")
    workspaces = _workspaces(tmp_path, origin, _clock())
    first = workspaces.prepare(SESSION)
    assert first.worktree_dir is not None

    run = SubprocessRunner()
    identity = ["-c", "user.name=x", "-c", "user.email=x@example.com"]
    wt = ["git", "-C", str(first.worktree_dir), *identity]
    (first.worktree_dir / "a.txt").write_text("planted\n")
    run.run([*wt, "commit", "--quiet", "-am", "planted"])
    planted = run.run([*wt, "rev-parse", "HEAD"]).stdout.strip()
    run.run([*wt, "update-ref", "refs/remotes/origin/main", planted])

    second = workspaces.prepare(OTHER)
    assert second.commit == commit
    assert second.snapshot_dir is not None
    assert (second.snapshot_dir / "a.txt").read_text() == "a\n"


def _in_worktree(path: Path) -> Origin:
    worktree = Origin.__new__(Origin)
    worktree.path = path
    worktree._run = SubprocessRunner()
    return worktree


def test_a_merged_session_branch_moves_to_the_new_main(tmp_path: Path, origin: Origin) -> None:
    origin.commit("README.md", "hello\n")
    clock = _clock()
    workspaces = _workspaces(tmp_path, origin, clock)
    ws = workspaces.prepare(SESSION)
    assert ws.worktree_dir is not None
    _in_worktree(ws.worktree_dir).commit("docs/a.md", "a\n")

    # Squash merge: the same file lands on main as a different commit, then main moves on.
    origin.commit("docs/a.md", "a\n")
    main = origin.commit("b.txt", "b\n")
    clock.advance(timedelta(minutes=5))
    again = workspaces.prepare(SESSION)

    assert again.commit == main
    worktree = _in_worktree(ws.worktree_dir)
    assert worktree.git("rev-parse", "HEAD") == main
    assert worktree.git("symbolic-ref", "HEAD") == f"refs/heads/session/{SESSION}"
    assert (ws.worktree_dir / "b.txt").read_text() == "b\n"


def test_a_clean_session_without_commits_follows_main(tmp_path: Path, origin: Origin) -> None:
    origin.commit("README.md", "hello\n")
    clock = _clock()
    workspaces = _workspaces(tmp_path, origin, clock)
    ws = workspaces.prepare(SESSION)
    assert ws.worktree_dir is not None

    main = origin.commit("b.txt", "b\n")
    clock.advance(timedelta(minutes=5))
    workspaces.prepare(SESSION)

    assert _in_worktree(ws.worktree_dir).git("rev-parse", "HEAD") == main


def test_unmerged_commits_stay_where_they_are(tmp_path: Path, origin: Origin) -> None:
    origin.commit("README.md", "hello\n")
    clock = _clock()
    workspaces = _workspaces(tmp_path, origin, clock)
    ws = workspaces.prepare(SESSION)
    assert ws.worktree_dir is not None
    mine = _in_worktree(ws.worktree_dir).commit("docs/a.md", "mine\n")

    origin.commit("docs/a.md", "theirs\n")
    clock.advance(timedelta(minutes=5))
    workspaces.prepare(SESSION)

    assert _in_worktree(ws.worktree_dir).git("rev-parse", "HEAD") == mine
    assert (ws.worktree_dir / "docs" / "a.md").read_text() == "mine\n"


def test_uncommitted_changes_keep_the_branch_in_place(tmp_path: Path, origin: Origin) -> None:
    first = origin.commit("README.md", "hello\n")
    clock = _clock()
    workspaces = _workspaces(tmp_path, origin, clock)
    ws = workspaces.prepare(SESSION)
    assert ws.worktree_dir is not None
    (ws.worktree_dir / "README.md").write_text("edited\n")

    origin.commit("b.txt", "b\n")
    clock.advance(timedelta(minutes=5))
    workspaces.prepare(SESSION)

    assert _in_worktree(ws.worktree_dir).git("rev-parse", "HEAD") == first
    assert (ws.worktree_dir / "README.md").read_text() == "edited\n"
