"""Daily conversation summaries in the owner's vault (ADR-0035).

Every round core
- settles running summaries: a finished summarizer task is written to
  ``Private/enikk/대화/YYYY-MM-DD.md``; a failed one is reported to the owner in one line;
- starts the days that are due (yesterday after ``run_hour``, missed days within
  ``catchup_days``, failed days once more on a later day): it collects the day's
  conversations, masks secrets and queues a task for the summarizer role. A day
  without conversations gets no file.

The owner may ask for a day by hand (``request``); that replaces the day's note.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import TYPE_CHECKING, Final

from enikk_contracts.agents import PlatformTool
from enikk_contracts.common import ConnectorKind, ConversationRef
from enikk_contracts.digests import DigestAccepted, DigestFailed, DigestStatus
from enikk_contracts.tasks import TaskStatus
from enikk_core_domain.digests import (
    ConversationDigest,
    DigestSchedule,
    day_threads,
    summary_body,
    summary_input,
)
from enikk_core_domain.errors import (
    InvalidRequestError,
    NotFoundError,
    PermissionDeniedError,
    UnknownAgentError,
)
from enikk_core_domain.model import Notice, Session, Task
from enikk_kernel.models import FrozenModel

if TYPE_CHECKING:
    import uuid
    from datetime import date, datetime

    from enikk_contracts.digests import DigestRequest
    from enikk_core.memory import MemoryService
    from enikk_core_domain.digests import DayThread
    from enikk_core_domain.ports import AgentCatalog, UnitOfWork, UnitOfWorkFactory
    from enikk_kernel.clock import Clock
    from enikk_kernel.ids import IdGenerator

CHANNEL: Final = "digest"
EVENT_PREFIX: Final = "digest:"
MAX_RECORDS: Final = 2_000
"""Tasks and messages read for one day, each."""

_log = logging.getLogger(__name__)


class DigestPolicy(FrozenModel):
    schedule: DigestSchedule = DigestSchedule()
    agent: str = "summarizer"
    max_task_attempts: int = 3


class DigestService:
    def __init__(
        self,
        *,
        uow: UnitOfWorkFactory,
        catalog: AgentCatalog,
        memory: MemoryService,
        clock: Clock,
        ids: IdGenerator,
        policy: DigestPolicy,
    ) -> None:
        self._uow = uow
        self._catalog = catalog
        self._memory = memory
        self._clock = clock
        self._ids = ids
        self._policy = policy

    @property
    def schedule(self) -> DigestSchedule:
        return self._policy.schedule

    async def run_once(self) -> None:
        await self.settle()
        await self.start_due()

    # ------------------------------------------------------------ starting

    async def start_due(self) -> list[date]:
        """Start every due day. Returns the days started (with or without a task)."""
        started: list[date] = []
        for day in self.schedule.due_days(self._clock.now()):
            async with self._uow() as uow:
                current = await uow.digests.get_for_update(day)
                if current is not None and not current.due(self.schedule, self._clock.now()):
                    continue
                if await self._start(uow, day, current, manual=False) is None:
                    return started
                await uow.commit()
            started.append(day)
        return started

    async def request(self, task_id: uuid.UUID, request: DigestRequest) -> DigestAccepted:
        """The owner's request, from a task they started in Slack: (re)make ``day``."""
        await self._from_owner(task_id)
        today = self.schedule.today(self._clock.now())
        if request.day > today:
            raise InvalidRequestError(f"{request.day} has not come yet (today is {today})")
        async with self._uow() as uow:
            current = await uow.digests.get_for_update(request.day)
            if current is not None and current.status is DigestStatus.RUNNING:
                return DigestAccepted(
                    day=request.day,
                    status=current.status,
                    detail="이미 이 날의 요약을 만드는 중입니다.",
                )
            digest = await self._start(uow, request.day, current, manual=True)
            if digest is None:
                raise UnknownAgentError(f"role '{self._policy.agent}' is not defined")
            await uow.commit()
        _log.info("conversation summary of %s requested by the owner", request.day)
        detail = (
            "대화가 없어 파일을 만들지 않았습니다."
            if digest.status is DigestStatus.EMPTY
            else "요약을 만들기 시작했습니다. 같은 날의 노트가 있으면 새 요약으로 바뀝니다."
        )
        return DigestAccepted(day=request.day, status=digest.status, detail=detail)

    async def _start(
        self,
        uow: UnitOfWork,
        day: date,
        current: ConversationDigest | None,
        *,
        manual: bool,
    ) -> ConversationDigest | None:
        """Queue the summarizer for ``day``, or record an empty day. ``None`` without the role."""
        definition = self._catalog.get(self._policy.agent)
        if definition is None:
            _log.warning("no '%s' role: conversation summaries are off", self._policy.agent)
            return None
        now = self._clock.now()
        threads = await self._threads(uow, day)
        task_id = None
        if threads:
            attempt = (current.attempts if current is not None else 0) + 1
            session = Session.start(
                session_id=self._ids.new(),
                conversation=ConversationRef(
                    connector=ConnectorKind.SCHEDULE,
                    channel=CHANNEL,
                    thread=f"{day.isoformat()}#{attempt}",
                ),
                agent=definition,
                now=now,
            )
            await uow.sessions.add(session)
            task = Task.enqueue(
                task_id=self._ids.new(),
                session=session,
                source_event_id=f"{EVENT_PREFIX}{day.isoformat()}:{attempt}",
                text=summary_input(day, threads, self.schedule),
                max_attempts=self._policy.max_task_attempts,
                now=now,
                execution=definition.execution,
            )
            await uow.tasks.add(task)
            task_id = task.id
        if current is None:
            digest = ConversationDigest.first(day, task_id=task_id, now=now, manual=manual)
            await uow.digests.add(digest)
        else:
            digest = current.again(task_id=task_id, now=now, manual=manual)
            await uow.digests.update(digest)
        _log.info(
            "conversation summary of %s: %s",
            day,
            f"{len(threads)} thread(s), task {task_id}" if threads else "no conversations",
        )
        return digest

    async def _threads(self, uow: UnitOfWork, day: date) -> list[DayThread]:
        return await day_conversations(uow, self.schedule, day)

    async def _from_owner(self, task_id: uuid.UUID) -> None:
        async with self._uow() as uow:
            task = await uow.tasks.get_for_update(task_id)
            session = None if task is None else await uow.sessions.get(task.session_id)
        if task is None or session is None:
            raise NotFoundError(f"task {task_id} not found")
        definition = self._catalog.get(task.agent)
        if definition is None or PlatformTool.CONVERSATION_DIGEST not in definition.tools.platform:
            raise PermissionDeniedError(f"role '{task.agent}' may not ask for summaries")
        if session.conversation.connector is not ConnectorKind.SLACK:
            raise PermissionDeniedError("only the owner, in Slack, asks for a summary")

    # ------------------------------------------------------------ settling

    async def settle(self) -> int:
        """Write finished summaries and report failed ones. Returns how many settled."""
        async with self._uow() as uow:
            running = list(await uow.digests.in_status(DigestStatus.RUNNING))
        settled = 0
        for digest in running:
            if digest.task_id is None:
                continue
            async with self._uow() as uow:
                task = await uow.tasks.get_for_update(digest.task_id)
            if task is None:
                await self._fail(digest, "the summarizer task is missing")
            elif task.status is TaskStatus.SUCCEEDED and task.output is not None:
                await self._write(digest, task.output)
            elif task.terminal:
                await self._fail(digest, task.failure or f"the summarizer task was {task.status}")
            else:
                continue
            settled += 1
        return settled

    async def _write(self, digest: ConversationDigest, summary: str) -> None:
        async with self._uow() as uow:
            threads = await self._threads(uow, digest.day)
        try:
            path = await self._memory.write_conversations(
                digest.day,
                f"{digest.day.isoformat()} 대화",
                summary_body(summary, threads),
                len(threads),
            )
        except Exception as exc:
            _log.warning("could not write the summary of %s: %s", digest.day, exc)
            await self._fail(digest, f"could not write the note: {exc}")
            return
        async with self._uow() as uow:
            current = await uow.digests.get_for_update(digest.day)
            if current is None or current.task_id != digest.task_id:
                return
            await uow.digests.update(current.written(self._clock.now()))
            await uow.commit()
        _log.info("conversation summary of %s written to %s", digest.day, path)

    async def _fail(self, digest: ConversationDigest, reason: str) -> None:
        now = self._clock.now()
        async with self._uow() as uow:
            current = await uow.digests.get_for_update(digest.day)
            if current is None or current.task_id != digest.task_id:
                return
            failed = current.failed(reason, now)
            await uow.digests.update(failed)
            retry = self._retried(failed, now)
            await uow.notices.add(
                Notice(
                    id=self._ids.new(),
                    body=DigestFailed(day=digest.day, reason=reason[:500], retry=retry),
                    created_at=now,
                )
            )
            await uow.commit()
        _log.warning("conversation summary of %s failed: %s", digest.day, reason)

    def _retried(self, failed: ConversationDigest, now: datetime) -> bool:
        """Whether the next day's run will try ``failed`` again (see ``retry_due``)."""
        oldest_tomorrow = self.schedule.today(now) - timedelta(days=self.schedule.catchup_days - 1)
        return failed.attempts < self.schedule.max_auto_attempts and failed.day >= oldest_tomorrow


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
