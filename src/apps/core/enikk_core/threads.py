"""An agent's pull request in the Slack thread that asked for it (ADR-0025, ADR-0036).

The developer works on ``agent/<developer session ID>``; that session replies into
the thread that delegated to it. So the branch name alone leads back to the thread.
Older branches were ``agent/<task ID>``; their task leads to the session.

The PR's progress is one status card in that thread, updated in place.
"""

from __future__ import annotations

import logging
import re
import uuid
from typing import TYPE_CHECKING, Final

from pydantic import ValidationError

from enikk_contracts.approvals import PullStage, PullStatusCard
from enikk_contracts.outbox import OutboundKind
from enikk_contracts.tasks import TaskStatus
from enikk_contracts.threads import ReviewThreadLinked
from enikk_core_domain.model import Notice, OutboxMessage, ReviewMessageRef

if TYPE_CHECKING:
    from enikk_contracts.threads import ReviewThreadOpened
    from enikk_core_domain.model import Session, Task
    from enikk_core_domain.ports import UnitOfWork, UnitOfWorkFactory
    from enikk_kernel.clock import Clock
    from enikk_kernel.ids import IdGenerator

AGENT_PREFIX: Final = "agent/"
MAX_NOTE: Final = 3_000
MAX_PULL_LINKS: Final = 3
PULL_URL: Final = re.compile(r"https://github\.com/[\w.-]+/[\w.-]+/pull/\d+")

_log = logging.getLogger(__name__)


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
        found = await self.origin(uow, branch)
        if found is None:
            return False
        session, latest = found
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

    async def card(
        self,
        uow: UnitOfWork,
        branch: str,
        *,
        repository: str,
        number: int,
        url: str,
        title: str,
        stage: PullStage,
        commit: str | None = None,
        detail: str = "",
    ) -> bool:
        """Queue the PR's status card for the thread behind ``branch``, posted once and
        then updated. False if there is no such thread."""
        found = await self.origin(uow, branch)
        if found is None:
            return False
        session, _ = found
        if session.reply_to is None:
            return False
        try:
            body = PullStatusCard(
                repository=repository,
                number=number,
                url=url[:500],
                title=title[:300],
                origin=session.reply_to,
                stage=stage,
                commit=commit,
                detail=detail[:MAX_NOTE],
            )
        except ValidationError as exc:
            _log.warning("no status card for %s#%d: %s", repository, number, exc)
            return False
        await uow.notices.add(Notice(id=self._ids.new(), body=body, created_at=self._clock.now()))
        return True

    async def origin(self, uow: UnitOfWork, branch: str) -> tuple[Session, Task] | None:
        """The delegated session behind ``branch`` and its most recent task."""
        session = await self._session(uow, branch)
        if session is None or session.reply_to is None:
            return None
        latest = await uow.tasks.latest_in_session(session.id)
        if latest is None:
            return None
        return session, latest

    async def _session(self, uow: UnitOfWork, branch: str) -> Session | None:
        owner = branch_owner(branch)
        if owner is None:
            return None
        session = await uow.sessions.get(owner)
        if session is not None:
            return session
        task = await uow.tasks.get_for_update(owner)
        return None if task is None else await uow.sessions.get(task.session_id)


class ReviewLinks:
    """Links the approval message of an agent PR with the thread that asked for it.

    The asking thread's status card links to the approval message (core hands the card
    out with that message, ADR-0036); the approval thread gets back where the request
    came from and the agent's report, which stays out of the asking thread.
    """

    def __init__(self, uow: UnitOfWorkFactory, notes: ThreadNotes) -> None:
        self._uow = uow
        self._notes = notes

    async def opened(self, review: ReviewThreadOpened) -> ReviewThreadLinked:
        async with self._uow() as uow:
            if review.channel is not None and review.ts is not None:
                await uow.review_messages.put(
                    ReviewMessageRef(
                        repository=review.repository,
                        number=review.number,
                        channel=review.channel,
                        ts=review.ts,
                    )
                )
                await uow.commit()
            found = await self._notes.origin(uow, review.branch)
            if found is None:
                return ReviewThreadLinked()
            session, latest = found
            if review.updated:
                return ReviewThreadLinked(origin=session.reply_to)
        _log.info("linked the review thread of %s#%d", review.repository, review.number)
        report = latest.output if latest.status is TaskStatus.SUCCEEDED else None
        return ReviewThreadLinked(origin=session.reply_to, report=report)


def delegated_status(agent: str, output: str) -> str:
    """What the asking thread sees of a delegated agent's answer.

    An answer that opened or updated a pull request becomes a status line with its link;
    the full answer goes to the owner's review thread. Other answers are kept as they are.
    """
    links = list(dict.fromkeys(PULL_URL.findall(output)))[:MAX_PULL_LINKS]
    if not links:
        return output
    return (
        f"에닉의 {agent} 역할이 작업을 마쳤습니다. PR: {' '.join(links)}\n"
        "검사가 끝나면 승인 채널에 승인 요청이 올라가고, 이 스레드에는 PR 상태 카드가 생깁니다. "
        "바뀐 내용과 상세는 승인 채널의 스레드에 있습니다."
    )
