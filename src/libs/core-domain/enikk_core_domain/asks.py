"""Requests one agent makes of another, run only after the owner allows them on a card
in the asking thread (ADR-0054)."""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Annotated, Self

from pydantic import AwareDatetime, Field

from enikk_contracts.common import AgentName, Identifier
from enikk_contracts.delegation import MAX_REQUEST_CHARS, AskStatus
from enikk_core_domain.errors import InvalidTransitionError
from enikk_core_domain.model import Entity

if TYPE_CHECKING:
    from datetime import datetime


class AskedWork(Entity):
    """One request of ``requester``'s thread session for ``agent``'s work."""

    id: uuid.UUID
    task_id: uuid.UUID
    """The task that asked; its session is the asking thread."""
    requester: AgentName
    agent: AgentName
    request: Annotated[str, Field(min_length=1, max_length=MAX_REQUEST_CHARS)]
    reason: Annotated[str, Field(min_length=1, max_length=500)]
    status: AskStatus = AskStatus.REQUESTED
    created_at: AwareDatetime
    expires_at: AwareDatetime
    decided_at: AwareDatetime | None = None
    decided_by: Identifier | None = None
    queued_task_id: uuid.UUID | None = None
    """The asked agent's task, once the owner allowed it."""

    def expired(self, now: datetime) -> bool:
        return self.status is AskStatus.REQUESTED and now >= self.expires_at

    def approve(self, now: datetime, by: str, queued_task_id: uuid.UUID) -> Self:
        self._require_open(now)
        return self._evolve(
            status=AskStatus.APPROVED, decided_at=now, decided_by=by, queued_task_id=queued_task_id
        )

    def reject(self, now: datetime, by: str) -> Self:
        self._require_open(now)
        return self._evolve(status=AskStatus.REJECTED, decided_at=now, decided_by=by)

    def expire(self, now: datetime) -> Self:
        if not self.expired(now):
            raise InvalidTransitionError(f"request {self.id} is {self.status}, not due to expire")
        return self._evolve(status=AskStatus.EXPIRED, decided_at=now)

    def _require_open(self, now: datetime) -> None:
        if self.status is not AskStatus.REQUESTED or now >= self.expires_at:
            raise InvalidTransitionError(f"request {self.id} is no longer open")
