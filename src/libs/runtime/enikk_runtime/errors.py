"""Failures that end a task. ``reason`` is shown to the owner, so it never holds secrets."""

from __future__ import annotations

from enikk_kernel.errors import EnikkError


class TaskFailedError(EnikkError):
    """The task cannot complete. ``retryable`` lets core queue it again."""

    def __init__(self, reason: str, *, retryable: bool) -> None:
        super().__init__(reason)
        self.reason = reason
        self.retryable = retryable


class PromptUnavailableError(TaskFailedError):
    """The prompt could not be fetched. Never replaced by a default prompt (I-13)."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason, retryable=True)


class IncompatibleModelError(TaskFailedError):
    """The prompt's model cannot run on this engine (I-16)."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason, retryable=False)


class EngineError(TaskFailedError):
    """Raised by engines. The engine decides whether a retry can help."""
