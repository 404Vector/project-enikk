"""Test doubles shared by every package's test suite."""

from __future__ import annotations

from typing import TYPE_CHECKING

from enikk_kernel.errors import ExternalCommandError
from enikk_kernel.models import FrozenModel
from enikk_kernel.process import ProcessResult

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path


class RecordedCall(FrozenModel):
    argv: tuple[str, ...]
    stdin: str | None
    mutating: bool
    stream: bool


class RecordingRunner:
    """In-memory ``ProcessRunner`` that records calls and replays scripted results.

    Scripted results are matched by argv prefix; the longest matching prefix wins.
    Unmatched calls succeed with empty output.
    """

    def __init__(self) -> None:
        self.calls: list[RecordedCall] = []
        self._scripted: dict[tuple[str, ...], ProcessResult] = {}

    def when(self, *prefix: str, stdout: str = "", stderr: str = "", returncode: int = 0) -> None:
        self._scripted[prefix] = ProcessResult(
            argv=prefix, returncode=returncode, stdout=stdout, stderr=stderr
        )

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
        command = tuple(argv)
        self.calls.append(RecordedCall(argv=command, stdin=stdin, mutating=mutating, stream=stream))
        result = self._match(command).model_copy(update={"argv": command})
        if check and not result.ok:
            raise ExternalCommandError(command, result.returncode, result.stderr)
        return result

    @property
    def argvs(self) -> list[tuple[str, ...]]:
        return [call.argv for call in self.calls]

    def _match(self, command: tuple[str, ...]) -> ProcessResult:
        best: tuple[str, ...] | None = None
        for prefix in self._scripted:
            if command[: len(prefix)] == prefix and (best is None or len(prefix) > len(best)):
                best = prefix
        if best is None:
            return ProcessResult(argv=command, returncode=0)
        return self._scripted[best]
