"""The session's GitHub token in the shell (ADR-0041 §1).

The engine writes the task's token into the run's home before the CLI starts: git
pushes to github.com with it through a credential file, and ``enikk-github`` calls the
GitHub API with it. The home is the run's own scratch directory and is removed when the
run ends. This is the one secret the sandboxed tools can read (I-37 exception); it
expires within an hour and cannot merge to ``main`` (I-33).
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Final

import httpx

if TYPE_CHECKING:
    from pydantic import SecretStr

TOKEN_FILE: Final = Path(".config/enikk/github-token")
CREDENTIALS_FILE: Final = Path(".git-credentials")
API: Final = "https://api.github.com"
TIMEOUT_SECONDS: Final = 30.0


def install(home: Path, token: SecretStr) -> None:
    """Make ``token`` the shell's GitHub credential in ``home``."""
    value = token.get_secret_value()
    credentials = home / CREDENTIALS_FILE
    _write_private(credentials, f"https://x-access-token:{value}@github.com\n")
    _write_private(home / TOKEN_FILE, value + "\n")
    (home / ".gitconfig").write_text(
        f'[credential "https://github.com"]\n\thelper = store --file={credentials}\n',
        encoding="utf-8",
    )


def _write_private(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(text)


def main(argv: list[str] | None = None) -> int:
    """``enikk-github METHOD PATH [--data FILE]``: one GitHub API call with the session's
    token. Prints the status line and the response body; exits 1 on an error status."""
    parser = argparse.ArgumentParser(prog="enikk-github", description=main.__doc__)
    parser.add_argument("method", choices=["GET", "POST", "PATCH", "PUT", "DELETE"])
    parser.add_argument("path", help="e.g. /repos/OWNER/REPO/pulls")
    parser.add_argument("--data", type=Path, help="a JSON file to send as the body")
    args = parser.parse_args(argv)
    token_file = Path.home() / TOKEN_FILE
    if not token_file.exists():
        print("no GitHub token in this session (ADR-0041 §1)", file=sys.stderr)
        return 2
    token = token_file.read_text(encoding="utf-8").strip()
    body = args.data.read_bytes() if args.data else None
    response = httpx.request(
        args.method,
        API + "/" + args.path.lstrip("/"),
        content=body,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            **({"Content-Type": "application/json"} if body else {}),
        },
        timeout=TIMEOUT_SECONDS,
    )
    print(f"HTTP {response.status_code}")
    print(response.text)
    return 0 if response.is_success else 1
