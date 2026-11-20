"""Process execution port and its subprocess-based implementation (P2, P8, P9).

Every external command in the project goes through a ``ProcessRunner`` so that
it can be logged, dry-run, and replaced in tests.
"""

from __future__ import annotations

import logging
import shlex
import shutil
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from enikk_core.errors import ExternalCommandError, ToolNotFoundError

_log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ProcessResult:
    args: tuple[str, ...]
    returncode: int
    stdout: str = ""
    stderr: str = ""

    @property
    def ok(self) -> bool:
        return self.returncode == 0


class ProcessRunner(Protocol):
    """Port for running external commands.

    Args:
        input: text written to stdin (use this for secrets, never argv).
        check: raise ``ExternalCommandError`` on non-zero exit.
        mutating: the command changes state; skipped under dry-run and logged at INFO.
        stream: inherit the terminal (stdin/stdout/stderr) instead of capturing.
            Required for interactive sessions and long-running progress output.
    """

    def run(
        self,
        args: Sequence[str],
        *,
        input: str | None = None,
        cwd: Path | None = None,
        check: bool = True,
        mutating: bool = True,
        stream: bool = False,
    ) -> ProcessResult: ...


class SubprocessRunner:
    """``ProcessRunner`` backed by :mod:`subprocess`."""

    def __init__(self, *, dry_run: bool = False) -> None:
        self._dry_run = dry_run

    @property
    def dry_run(self) -> bool:
        return self._dry_run

    def run(
        self,
        args: Sequence[str],
        *,
        input: str | None = None,
        cwd: Path | None = None,
        check: bool = True,
        mutating: bool = True,
        stream: bool = False,
    ) -> ProcessResult:
        if not args:
            raise ValueError("args must not be empty")
        argv = tuple(args)
        self._ensure_executable(argv[0])

        rendered = shlex.join(argv)
        if mutating and self._dry_run:
            _log.info("[dry-run] $ %s", rendered)
            return ProcessResult(argv, 0)
        _log.log(logging.INFO if mutating else logging.DEBUG, "$ %s", rendered)

        completed = subprocess.run(  # noqa: S603 - argv is never passed through a shell
            list(argv),
            input=input,
            cwd=cwd,
            text=True,
            capture_output=not stream,
            check=False,
        )
        result = ProcessResult(
            argv,
            completed.returncode,
            completed.stdout or "",
            completed.stderr or "",
        )
        if check and not result.ok:
            raise ExternalCommandError(argv, result.returncode, result.stderr)
        return result

    @staticmethod
    def _ensure_executable(name: str) -> None:
        if shutil.which(name) is None:
            raise ToolNotFoundError(name)
