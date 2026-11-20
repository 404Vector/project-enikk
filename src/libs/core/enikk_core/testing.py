"""Test doubles shared by every package's test suite (P8)."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from enikk_core.errors import ExternalCommandError
from enikk_core.process import ProcessResult


@dataclass(frozen=True, slots=True)
class RecordedCall:
    args: tuple[str, ...]
    input: str | None
    mutating: bool
    stream: bool


@dataclass
class RecordingRunner:
    """In-memory ``ProcessRunner`` that records calls and replays scripted results.

    Responses are matched by argv prefix; the longest matching prefix wins.
    """

    calls: list[RecordedCall] = field(default_factory=list)
    _responses: dict[tuple[str, ...], ProcessResult] = field(default_factory=dict)

    def when(self, *prefix: str, stdout: str = "", stderr: str = "", returncode: int = 0) -> None:
        self._responses[prefix] = ProcessResult(prefix, returncode, stdout, stderr)

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
        argv = tuple(args)
        self.calls.append(RecordedCall(argv, input, mutating, stream))
        scripted = self._match(argv)
        result = ProcessResult(argv, scripted.returncode, scripted.stdout, scripted.stderr)
        if check and not result.ok:
            raise ExternalCommandError(argv, result.returncode, result.stderr)
        return result

    @property
    def argvs(self) -> list[tuple[str, ...]]:
        return [c.args for c in self.calls]

    def _match(self, argv: tuple[str, ...]) -> ProcessResult:
        best: tuple[str, ...] | None = None
        for prefix in self._responses:
            if argv[: len(prefix)] == prefix and (best is None or len(prefix) > len(best)):
                best = prefix
        return self._responses[best] if best is not None else ProcessResult(argv, 0)
