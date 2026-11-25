"""The session's GitHub token in the run's home (ADR-0041 §1)."""

from __future__ import annotations

import stat
from typing import TYPE_CHECKING

from pydantic import SecretStr

from enikk_engine_claude.github_access import CREDENTIALS_FILE, TOKEN_FILE, install, main

if TYPE_CHECKING:
    from pathlib import Path

    import pytest


def test_install_writes_private_credentials_and_the_git_helper(tmp_path: Path) -> None:
    install(tmp_path, SecretStr("ghs-test"))
    credentials = tmp_path / CREDENTIALS_FILE
    assert credentials.read_text() == "https://x-access-token:ghs-test@github.com\n"
    assert (tmp_path / TOKEN_FILE).read_text() == "ghs-test\n"
    for path in (credentials, tmp_path / TOKEN_FILE):
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    gitconfig = (tmp_path / ".gitconfig").read_text()
    assert '[credential "https://github.com"]' in gitconfig
    assert f"helper = store --file={credentials}" in gitconfig
    assert "ghs-test" not in gitconfig


def test_the_api_helper_needs_a_token(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    assert main(["GET", "/repos/o/r/pulls"]) == 2
