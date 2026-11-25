"""The day's conversations (ADR-0035), the input of schedules that summarize a day
(ADR-0039 S7). The daily summary itself is a schedule now; the loop that made it
is gone (S7b).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from enikk_contracts.common import ConnectorKind
from enikk_core_domain.digests import day_threads

if TYPE_CHECKING:
    import uuid
    from datetime import date

    from enikk_core_domain.digests import DayThread, DigestSchedule
    from enikk_core_domain.model import Session, Task
    from enikk_core_domain.ports import UnitOfWork

MAX_RECORDS: Final = 2_000


def _from_conversation(
    task_id: uuid.UUID, owners: dict[uuid.UUID, Task], sessions: dict[uuid.UUID, Session]
) -> bool:
    """Whether a message belongs to a conversation (not to core's scheduled work)."""
    task = owners.get(task_id)
    if task is None:
        return True
    session = sessions.get(task.session_id)
    return session is None or session.conversation.connector is ConnectorKind.SLACK


async def day_conversations(
    uow: UnitOfWork, schedule: DigestSchedule, day: date
) -> list[DayThread]:
    """The conversations of ``day`` in ``schedule``'s time zone (ADR-0035), for the
    summary and for schedules whose input is ``day-conversations`` (ADR-0039 S7)."""
    start, end = schedule.bounds(day)
    tasks = await uow.tasks.created_between(start, end, MAX_RECORDS)
    messages = await uow.outbox.created_between(start, end, MAX_RECORDS)
    owners = {t.id: t for t in tasks}
    for message in messages:
        if message.task_id not in owners:
            found = await uow.tasks.get_for_update(message.task_id)
            if found is not None:
                owners[found.id] = found
    sessions = {}
    for session_id in {t.session_id for t in owners.values()}:
        session = await uow.sessions.get(session_id)
        if session is not None:
            sessions[session_id] = session
    agents_of = {t.id: t.agent for t in owners.values()}
    visible = [m for m in messages if _from_conversation(m.task_id, owners, sessions)]
    return day_threads(tasks, sessions, visible, agents_of)
