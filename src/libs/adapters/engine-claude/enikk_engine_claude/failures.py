"""What went wrong in one Claude Code run, for the runtime log and the task (ADR-0034).

The reason reaches the owner's Slack thread, the detail the runtime log. Both are
masked with the kernel's secret pattern and cut short. A broken CLI process's message
goes to the log only.
"""

from __future__ import annotations

from collections import deque
from typing import TYPE_CHECKING, Any, Final

from claude_agent_sdk import ResultError

from enikk_kernel.models import FrozenModel
from enikk_kernel.redaction import redact

if TYPE_CHECKING:
    from claude_agent_sdk import ClaudeSDKError, ResultMessage

    from enikk_runtime.model import SkillUse

NOT_RETRYABLE_RESULTS: Final = frozenset({"error_max_turns", "error_max_budget_usd"})
NOT_RETRYABLE_STATUS: Final = frozenset({400, 401, 403, 404, 413})
STDERR_LINES: Final = 20
MAX_CAUSE: Final = 300
MAX_DETAIL: Final = 4_000


class RunTrace:
    """What one run has shown so far; ``failure_of`` reads it when the run fails."""

    def __init__(self) -> None:
        self.texts: list[str] = []
        self.model: str | None = None
        self.result: ResultMessage | None = None
        self.last_tool: str | None = None
        self.answered = False
        """Whether the model produced a turn that was not an API error."""
        self.stderr: deque[str] = deque(maxlen=STDERR_LINES)
        self.skills: list[SkillUse] = []
        """Skills the run loaded (ADR-0037 §3)."""

    def stderr_line(self, line: str) -> None:
        self.stderr.append(redact(line.rstrip())[:MAX_CAUSE])


class Failure(FrozenModel):
    reason: str
    """Shown to the owner (task failure and Slack); one line."""
    detail: str
    """For the runtime log."""
    retryable: bool
    """Whether running the same input again may succeed."""


def failure_of_result(result: ResultMessage | None, trace: RunTrace) -> Failure:
    """The run ended with an error result, or with none at all."""
    if result is None:
        return _failure(trace, {"subtype": "no_result"}, "Claude Code ended without a result", True)
    fields = {
        "subtype": result.subtype,
        "is_error": result.is_error,
        "api_error_status": result.api_error_status,
        "terminal_reason": result.terminal_reason,
        "errors": result.errors or [],
        "result": result.result,
    }
    return _from_fields(fields, trace)


def failure_of_exception(exc: ClaudeSDKError, trace: RunTrace) -> Failure:
    """The SDK raised: an error result (``ResultError``) or a broken CLI process."""
    if isinstance(exc, ResultError):
        fields = {
            "subtype": exc.subtype,
            "is_error": exc.data.get("is_error"),
            "api_error_status": exc.api_error_status,
            "terminal_reason": exc.terminal_reason,
            "errors": exc.errors,
            "result": exc.result,
        }
        return _from_fields(fields, trace)
    reason = f"Claude Code process failed ({type(exc).__name__})"
    fields = {"error": type(exc).__name__, "message": _first_line(str(exc))}
    return _failure(trace, fields, reason, True)


def _from_fields(fields: dict[str, Any], trace: RunTrace) -> Failure:
    subtype = fields["subtype"]
    status = fields["api_error_status"]
    errors = [e for e in fields["errors"] if isinstance(e, str)]
    cause = _first_line(errors[0] if errors else fields["result"] or "")
    reason = f"Claude stopped: {subtype}"
    if status is not None:
        reason += f", API status {status}"
    if cause:
        reason += f": {cause}"
    return _failure(trace, fields, reason, _retryable(subtype, status, fields["terminal_reason"]))


def _retryable(subtype: object, status: object, terminal_reason: object) -> bool:
    """Only an API failure that may pass is worth the same input again; anything the
    CLI decided (a refused resume, the turn limit) fails the same way every time."""
    if subtype in NOT_RETRYABLE_RESULTS:
        return False
    if isinstance(status, int):
        return status not in NOT_RETRYABLE_STATUS
    return terminal_reason == "api_error"


def _failure(trace: RunTrace, fields: dict[str, Any], reason: str, retryable: bool) -> Failure:
    if trace.last_tool is not None:
        reason += f" (last tool {trace.last_tool})"
    parts = [f"{k}={v!r}" for k, v in fields.items()]
    parts.append(f"last_tool={trace.last_tool!r}")
    parts.append("stderr=" + (" | ".join(trace.stderr) or "(none)"))
    return Failure(
        reason=redact(reason),
        detail=redact(", ".join(parts))[:MAX_DETAIL],
        retryable=retryable,
    )


def _first_line(text: str) -> str:
    line = next((s.strip() for s in text.splitlines() if s.strip()), "")
    if len(line) > MAX_CAUSE:
        line = line[: MAX_CAUSE - 1] + "…"
    return redact(line)
