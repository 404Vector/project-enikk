"""Test doubles shared by every package's test suite."""

from __future__ import annotations

import uuid
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from enikk_kernel.errors import ExternalCommandError
from enikk_kernel.models import FrozenModel
from enikk_kernel.process import ProcessResult

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence
    from pathlib import Path


class ManualClock:
    """``Clock`` whose time only moves when the test advances it."""

    def __init__(self, start: datetime | None = None) -> None:
        self._now = start or datetime(2026, 1, 1, tzinfo=UTC)
        if self._now.tzinfo is None:
            raise ValueError("ManualClock requires a timezone-aware start time")

    def now(self) -> datetime:
        return self._now

    def advance(self, delta: timedelta) -> None:
        self._now += delta


class SequentialIds:
    """``IdGenerator`` that returns predictable UUIDs: 1, 2, 3, ..."""

    def __init__(self) -> None:
        self._next = 1

    def new(self) -> uuid.UUID:
        value = uuid.UUID(int=self._next)
        self._next += 1
        return value


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
        self.background_calls: list[tuple[str, ...]] = []
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

    @contextmanager
    def background(self, argv: Sequence[str]) -> Iterator[None]:
        self.background_calls.append(tuple(argv))
        yield

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
