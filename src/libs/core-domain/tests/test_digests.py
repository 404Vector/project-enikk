"""When days are summarized and what the summarizer reads (ADR-0035)."""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime

import pytest

from enikk_contracts.common import ConnectorKind, ConversationRef
from enikk_contracts.digests import DigestStatus
from enikk_contracts.outbox import OutboundKind
from enikk_core_domain.digests import (
    MAX_INPUT,
    ConversationDigest,
    DayEntry,
    DayThread,
    DigestSchedule,
    day_threads,
    owner_text,
    summary_body,
    summary_input,
)
from enikk_core_domain.errors import InvalidTransitionError
from enikk_core_domain.model import OutboxMessage, Session, Task
from enikk_core_domain.testing import agent
from enikk_core_domain.thread_context import HEADER, SEPARATOR
from enikk_kernel.redaction import MASK

SCHEDULE = DigestSchedule()
DAY = date(2026, 11, 23)
SECRET = "ghp_" + "a1B2" * 9


def kst(hour: int, minute: int = 0, day: int = 23) -> datetime:
    """A time on 2026-11-``day`` in Asia/Seoul, as UTC."""
    return datetime(2026, 11, day, hour, minute, tzinfo=SCHEDULE.zone).astimezone(UTC)


def test_a_day_runs_from_midnight_to_midnight_in_seoul() -> None:
    start, end = SCHEDULE.bounds(DAY)
    assert start == datetime(2026, 11, 22, 15, tzinfo=UTC)
    assert end == datetime(2026, 11, 23, 15, tzinfo=UTC)
    assert SCHEDULE.today(datetime(2026, 11, 22, 14, 59, tzinfo=UTC)) == date(2026, 11, 22)
    assert SCHEDULE.today(datetime(2026, 11, 22, 15, 0, tzinfo=UTC)) == DAY


def test_yesterday_is_due_from_four_oclock_in_seoul() -> None:
    assert SCHEDULE.due_days(kst(3, 59, day=24)) == [date(2026, 11, 21), date(2026, 11, 22)]
    assert SCHEDULE.due_days(kst(4, 0, day=24)) == [date(2026, 11, 22), DAY]
    assert DigestSchedule(catchup_days=1).due_days(kst(23, 59, day=24)) == [DAY]


def test_an_unknown_time_zone_is_refused() -> None:
    with pytest.raises(ValueError, match="Mars"):
        DigestSchedule(timezone="Mars/Olympus")


def test_a_failed_day_is_tried_once_more_on_a_later_day() -> None:
    started = kst(4, 0, day=24)
    running = ConversationDigest.first(DAY, task_id=uuid.uuid4(), now=started, manual=False)
    assert not running.due(SCHEDULE, started)
    failed = running.failed("prompt missing", started)
    assert not failed.due(SCHEDULE, kst(23, 0, day=24))
    assert failed.due(SCHEDULE, kst(4, 0, day=25))
    again = failed.again(task_id=uuid.uuid4(), now=kst(4, 0, day=25), manual=False)
    assert again.attempts == 2
    assert not again.failed("still", kst(4, 1, day=25)).due(SCHEDULE, kst(4, 0, day=26))
    with pytest.raises(InvalidTransitionError):
        again.again(task_id=uuid.uuid4(), now=kst(5, 0, day=25), manual=True)


def test_a_day_made_before_it_ended_is_made_again_after() -> None:
    early = ConversationDigest.first(DAY, task_id=uuid.uuid4(), now=kst(15, 0), manual=True)
    assert not early.written(kst(15, 5)).settled(SCHEDULE)
    assert early.written(kst(15, 5)).due(SCHEDULE, kst(4, 0, day=24))
    late = ConversationDigest.first(DAY, task_id=None, now=kst(4, 0, day=24), manual=False)
    assert late.status is DigestStatus.EMPTY
    assert late.settled(SCHEDULE)


def _session(thread: str, connector: ConnectorKind = ConnectorKind.SLACK) -> Session:
    return Session.start(
        session_id=uuid.uuid4(),
        conversation=ConversationRef(connector=connector, channel="D1", thread=thread),
        agent=agent("assistant"),
        now=kst(9),
    )


def _task(session: Session, text: str, at: datetime, name: str = "assistant") -> Task:
    work = Task.enqueue(
        task_id=uuid.uuid4(),
        session=session,
        source_event_id=f"ev-{uuid.uuid4()}",
        text=text,
        max_attempts=1,
        now=at,
    )
    return work.model_copy(update={"agent": name})


def _posted(session: Session, work: Task, text: str, at: datetime) -> OutboxMessage:
    return OutboxMessage(
        id=uuid.uuid4(),
        task_id=work.id,
        source_event_id=work.source_event_id,
        conversation=session.outbox_conversation,
        kind=OutboundKind.REPLY,
        text=text,
        created_at=at,
    )


def test_delegated_work_is_shown_in_the_thread_it_came_from() -> None:
    main = _session("1.0")
    delegated = Session.start(
        session_id=uuid.uuid4(),
        conversation=main.conversation.model_copy(update={"thread": "1.0:developer"}),
        agent=agent("developer"),
        now=kst(9),
        reply_to=main.conversation,
    )
    smoke = _session("smoke", ConnectorKind.SMOKE)
    asked = _task(main, "요약 기능 만들어 줘", kst(9, 0))
    handed = _task(delegated, "ADR-0028 §5 구현", kst(9, 2), "developer")
    checked = _task(smoke, "smoke check", kst(9, 3))
    sessions = {s.id: s for s in (main, delegated, smoke)}
    messages = [
        _posted(main, asked, "개발 역할에 맡겼습니다", kst(9, 1)),
        _posted(delegated, handed, "PR #68 을 열었습니다", kst(10, 0)),
        _posted(smoke, checked, "ok", kst(9, 4)),
    ]
    agents = {t.id: t.agent for t in (asked, handed, checked)}
    [thread] = day_threads([asked, handed, checked], sessions, messages, agents)
    assert thread.conversation == main.conversation
    assert thread.roles == ("assistant", "developer")
    assert [e.who for e in thread.entries] == [
        "소유자",
        "에닉(assistant)",
        "developer 역할이 받은 요청",
        "에닉(developer)",
    ]


def test_the_thread_context_in_front_of_an_input_is_left_out() -> None:
    text = f"{HEADER}\n- 09:00 UTC\n배포 완료\n\n{SEPARATOR}\n고마워"
    assert owner_text(text) == "고마워"
    assert owner_text("그냥 메시지") == "그냥 메시지"


def _thread(*texts: str) -> DayThread:
    return DayThread(
        conversation=ConversationRef(connector=ConnectorKind.SLACK, channel="D1", thread="1.0"),
        roles=("assistant",),
        entries=tuple(DayEntry(at=kst(9, i), who="소유자", text=t) for i, t in enumerate(texts)),
    )


def test_secrets_never_reach_the_summarizer_or_the_note() -> None:
    text = summary_input(DAY, [_thread(f"토큰은 {SECRET} 이야")], SCHEDULE)
    assert SECRET not in text
    assert MASK in text
    assert "09:00 소유자" in text
    assert text.startswith("[대화 기록")
    body = summary_body(f"요약: 토큰 {SECRET} 를 등록함", [_thread("x")])
    assert SECRET not in body
    assert MASK in body
    assert "slack D1 1.0" in body


def test_a_long_day_is_cut_to_fit_the_input() -> None:
    threads = [_thread(*(["가" * 3_000] * 20)) for _ in range(5)]
    text = summary_input(DAY, threads, SCHEDULE)
    assert len(text) <= MAX_INPUT
    assert text.rstrip().endswith("[대화 기록 끝]")
