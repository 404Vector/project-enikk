"""Wake a Slack thread's session when its pull request or prompt candidate moves on
(ADR-0041 §4).

Core queues a task in the session that opened the PR or asked for the candidate, so
the thread carries its work on without the owner having to say "approved". The task
is an ordinary task of that session: it runs after the session's other tasks, shows
the [취소] button and is cancelled with the session (I-29).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Final

from enikk_contracts.approvals import PullStage
from enikk_contracts.common import ConnectorKind
from enikk_contracts.prompts import PromotionStatus
from enikk_core_domain.model import Task
from enikk_core_domain.thread_context import MAX_MESSAGES, with_thread_context

if TYPE_CHECKING:
    from enikk_core_domain.model import Session
    from enikk_core_domain.ports import AgentCatalog, UnitOfWork
    from enikk_kernel.clock import Clock
    from enikk_kernel.ids import IdGenerator

EVENT_PREFIX: Final = "core-event:"
"""Source event IDs of wake-up tasks; one task per event (the ID is unique)."""

HEADER: Final = "[이벤트]"
MAX_DETAIL: Final = 1_000

PULL_STAGES: Final[dict[PullStage, str]] = {
    PullStage.CHECKS_FAILED: "검사 실패",
    PullStage.MERGED: "머지됨",
    PullStage.BLOCKED: "승인했지만 GitHub 가 머지를 거부함",
    PullStage.REJECTED: "소유자가 반려함",
    PullStage.CLOSED: "닫힘",
    PullStage.DEPLOYED: "배포 성공",
    PullStage.DEPLOY_FAILED: "배포 실패",
    PullStage.BUILD_FAILED: "이미지 빌드 실패",
    PullStage.ROLLED_BACK: "되돌림 배포 완료",
    PullStage.ROLLBACK_FAILED: "되돌림 실패",
}
"""The stages that wake the session, as the event names them. Checking and awaiting
approval wait for the owner, so they do not."""

PROMOTION_STATES: Final[dict[PromotionStatus, str]] = {
    PromotionStatus.PROMOTED: "production 으로 승격됨",
    PromotionStatus.REJECTED: "소유자가 승격을 거절함",
    PromotionStatus.STALE: "요청 뒤 production 이 바뀌어 승격하지 않음",
    PromotionStatus.REVERTED: "이전 production 으로 되돌림",
}

GUIDE: Final = (
    "core 가 보낸 알림입니다. 소유자의 메시지가 아닙니다.\n"
    "이 스레드에서 하던 일에 다음 단계가 있으면 이어 가십시오. "
    "반려됐으면 멈추고 이유를 물으십시오. 배포 실패의 되돌리기는 소유자가 정합니다. "
    "이어 갈 일이 없으면 한 줄로 끝내십시오."
)

_log = logging.getLogger(__name__)


def pull_event_text(
    *,
    repository: str,
    number: int,
    title: str,
    stage: PullStage,
    commit: str | None,
    detail: str,
) -> str:
    lines = [
        f"{HEADER} PR #{number} {PULL_STAGES[stage]}",
        f"- 저장소: {repository}",
        f"- 제목: {title}",
    ]
    if commit:
        lines.append(f"- 커밋: {commit[:12]}")
    if stage is PullStage.MERGED:
        lines.append("- 이미지가 바뀌는 변경이면 배포 결과가 따로 옵니다.")
    if detail:
        lines.append(f"- 상세: {detail[:MAX_DETAIL]}")
    return "\n".join([*lines, "", GUIDE])


def promotion_event_text(prompt: str, version: int, status: PromotionStatus, detail: str) -> str:
    lines = [f"{HEADER} 프롬프트 `{prompt}` v{version} {PROMOTION_STATES[status]}"]
    if detail:
        lines.append(f"- 상세: {detail[:MAX_DETAIL]}")
    return "\n".join([*lines, "", GUIDE])


class SessionWaker:
    def __init__(
        self, catalog: AgentCatalog, clock: Clock, ids: IdGenerator, max_attempts: int
    ) -> None:
        self._catalog = catalog
        self._clock = clock
        self._ids = ids
        self._max_attempts = max_attempts

    async def wake(self, uow: UnitOfWork, session: Session, *, key: str, text: str) -> bool:
        """Queue ``text`` as a task of ``session``, once per ``key``. False if the session
        is not a Slack thread's own, its agent is gone, or the event was already queued."""
        if (
            session.reply_to is not None
            or session.conversation.connector is not ConnectorKind.SLACK
        ):
            _log.info("no wake-up for session %s (%s): not a Slack thread's own", session.id, key)
            return False
        event_id = f"{EVENT_PREFIX}{key}"[:255]
        if await uow.tasks.find_by_source_event(event_id) is not None:
            _log.info("no wake-up for session %s (%s): already woken", session.id, key)
            return False
        definition = self._catalog.get(session.agent)
        if definition is None:
            _log.warning("no wake-up for session %s: agent %s is gone", session.id, session.agent)
            return False
        previous = await uow.tasks.latest_in_session(session.id)
        if previous is not None:
            posted = await uow.outbox.posted_by_others(
                session.conversation, session.id, previous.created_at, MAX_MESSAGES
            )
            text = with_thread_context(text, posted)
        task = Task.enqueue(
            task_id=self._ids.new(),
            session=session,
            source_event_id=event_id,
            text=text,
            max_attempts=self._max_attempts,
            now=self._clock.now(),
            execution=definition.execution,
        )
        await uow.tasks.add(task)
        _log.info("session %s woken by %s (task %s)", session.id, key, task.id)
        return True
