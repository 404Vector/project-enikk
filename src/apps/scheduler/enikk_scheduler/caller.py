"""The calling task of an MCP request (ADR-0039 §2).

The gateway sets the session from core's token (ADR-0041 §3); older tokens' task ID and
lease travel too. Core checks them on every call; this module only reads them.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Final

from enikk_contracts.schedules import Caller

if TYPE_CHECKING:
    from collections.abc import Mapping

SESSION_HEADER: Final = "x-enikk-session-id"
TASK_HEADER: Final = "x-enikk-task-id"
LEASE_HEADER: Final = "x-enikk-lease-id"


class MissingCallerError(ValueError):
    """The request does not name a running task."""


def caller_from(headers: Mapping[str, str] | None) -> Caller:
    lowered = {key.lower(): value for key, value in (headers or {}).items()}
    session = lowered.get(SESSION_HEADER)
    if session:
        try:
            return Caller(session_id=uuid.UUID(session))
        except ValueError as exc:
            raise MissingCallerError(f"{SESSION_HEADER} must be a UUID") from exc
    try:
        task_id = uuid.UUID(lowered[TASK_HEADER])
        lease_id = uuid.UUID(lowered[LEASE_HEADER])
    except (KeyError, ValueError) as exc:
        raise MissingCallerError(
            f"the request must carry {TASK_HEADER} and {LEASE_HEADER} as UUIDs"
        ) from exc
    return Caller(task_id=task_id, lease_id=lease_id)
