"""Common base for adapters that shell out to a CLI tool."""

from __future__ import annotations

from abc import ABC
from typing import TYPE_CHECKING, Any, ClassVar

if TYPE_CHECKING:
    from enikk_core.process import ProcessResult, ProcessRunner


class CliTool(ABC):  # noqa: B024 - abstract by intent; subclasses only declare ``binary``
    binary: ClassVar[str]

    def __init__(self, runner: ProcessRunner, *, executable: str | None = None) -> None:
        self._runner = runner
        self._executable = executable or self.binary

    def _run(self, *args: str, **options: Any) -> ProcessResult:
        return self._runner.run([self._executable, *args], **options)
