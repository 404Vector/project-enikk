"""Common base for adapters that run a CLI tool."""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

if TYPE_CHECKING:
    from enikk_kernel.process import ProcessResult, ProcessRunner


class CliTool:
    """Base for adapters wrapping one executable. Subclasses set ``binary``."""

    binary: ClassVar[str]

    def __init__(self, runner: ProcessRunner, *, executable: str | None = None) -> None:
        self._runner = runner
        self._executable = executable or self.binary

    def _run(
        self,
        *args: str,
        stdin: str | None = None,
        check: bool = True,
        mutating: bool = True,
        stream: bool = False,
    ) -> ProcessResult:
        return self._runner.run(
            [self._executable, *args],
            stdin=stdin,
            check=check,
            mutating=mutating,
            stream=stream,
        )
