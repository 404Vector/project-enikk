"""Notes about an agent's pull request in the Slack thread that asked for it (ADR-0025).

The developer works on ``agent/<developer session ID>``; that session replies into
the thread that delegated to it. So the branch name alone leads back to the thread.
Older branches were ``agent/<task ID>``; their task leads to the session.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Final

from enikk_contracts.outbox import OutboundKind
from enikk_core_domain.model import OutboxMessage

if TYPE_CHECKING:
    from enikk_core_domain.model import Session
    from enikk_core_domain.ports import UnitOfWork
    from enikk_kernel.clock import Clock
    from enikk_kernel.ids import IdGenerator

AGENT_PREFIX: Final = "agent/"
MAX_NOTE: Final = 3_000


def branch_owner(branch: str) -> uuid.UUID | None:
    """The session (or, for older branches, task) ID in an agent branch name."""
    if not branch.startswith(AGENT_PREFIX):
        return None
    try:
        return uuid.UUID(branch.removeprefix(AGENT_PREFIX))
    except ValueError:
        return None


class ThreadNotes:
    def __init__(self, clock: Clock, ids: IdGenerator) -> None:
        self._clock = clock
        self._ids = ids

    async def post(self, uow: UnitOfWork, branch: str, text: str) -> bool:
        """Queue ``text`` for the thread behind ``branch``. False if there is none."""
        session = await self._session(uow, branch)
        if session is None or session.reply_to is None:
            return False
        latest = await uow.tasks.latest_in_session(session.id)
        if latest is None:
            return False
        await uow.outbox.add(
            OutboxMessage(
                id=self._ids.new(),
                task_id=latest.id,
                source_event_id=latest.source_event_id,
                conversation=session.outbox_conversation,
                kind=OutboundKind.REPLY,
                text=text[:MAX_NOTE],
                created_at=self._clock.now(),
            )
        )
        return True

    async def _session(self, uow: UnitOfWork, branch: str) -> Session | None:
        owner = branch_owner(branch)
        if owner is None:
            return None
        session = await uow.sessions.get(owner)
        if session is not None:
            return session
        task = await uow.tasks.get_for_update(owner)
        return None if task is None else await uow.sessions.get(task.session_id)
