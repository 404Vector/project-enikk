"""External command execution port and its subprocess implementation."""

from __future__ import annotations

import logging
import shlex
import shutil
import subprocess
from contextlib import AbstractContextManager, contextmanager
from typing import TYPE_CHECKING, Protocol

from enikk_kernel.errors import ExternalCommandError, ToolNotFoundError
from enikk_kernel.models import FrozenModel

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence
    from pathlib import Path

BACKGROUND_STOP_TIMEOUT = 5.0

_log = logging.getLogger(__name__)


class ProcessResult(FrozenModel):
    argv: tuple[str, ...]
    returncode: int
    stdout: str = ""
    stderr: str = ""

    @property
    def ok(self) -> bool:
        return self.returncode == 0


class ProcessRunner(Protocol):
    """Runs external commands.

    Args:
        stdin: text written to standard input. Use this for secrets, never argv.
        check: raise ``ExternalCommandError`` on a non-zero exit status.
        mutating: the command changes state. Skipped under dry-run and logged at INFO.
        stream: inherit the terminal instead of capturing output.
    """

    def run(
        self,
        argv: Sequence[str],
        *,
        stdin: str | None = None,
        cwd: Path | None = None,
        check: bool = True,
        mutating: bool = True,
        stream: bool = False,
    ) -> ProcessResult: ...

    def background(self, argv: Sequence[str]) -> AbstractContextManager[None]:
        """Run a long-lived, non-mutating command (e.g. a port-forward) while the block runs."""
        ...


class SubprocessRunner:
    """``ProcessRunner`` backed by :mod:`subprocess`. Never uses a shell."""

    def __init__(self, *, dry_run: bool = False) -> None:
        self._dry_run = dry_run

    def run(
        self,
        argv: Sequence[str],
        *,
        stdin: str | None = None,
        cwd: Path | None = None,
        check: bool = True,
        mutating: bool = True,
        stream: bool = False,
    ) -> ProcessResult:
        if not argv:
            raise ValueError("argv must not be empty")
        command = tuple(argv)
        if shutil.which(command[0]) is None:
            raise ToolNotFoundError(command[0])

        rendered = shlex.join(command)
        if mutating and self._dry_run:
            _log.info("[dry-run] $ %s", rendered)
            return ProcessResult(argv=command, returncode=0)
        _log.log(logging.INFO if mutating else logging.DEBUG, "$ %s", rendered)

        completed = subprocess.run(
            list(command),
            input=stdin,
            cwd=cwd,
            text=True,
            capture_output=not stream,
            check=False,
        )
        result = ProcessResult(
            argv=command,
            returncode=completed.returncode,
            stdout=completed.stdout or "",
            stderr=completed.stderr or "",
        )
        if check and not result.ok:
            raise ExternalCommandError(command, result.returncode, result.stderr)
        return result

    @contextmanager
    def background(self, argv: Sequence[str]) -> Iterator[None]:
        if not argv:
            raise ValueError("argv must not be empty")
        command = tuple(argv)
        if shutil.which(command[0]) is None:
            raise ToolNotFoundError(command[0])
        _log.debug("$ %s &", shlex.join(command))
        process = subprocess.Popen(
            list(command), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        try:
            yield
        finally:
            process.terminate()
            try:
                process.wait(timeout=BACKGROUND_STOP_TIMEOUT)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
