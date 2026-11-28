"""An agent asks another for work; the owner allows it in the thread (ADR-0054)."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import timedelta

import pytest

from enikk_contracts.common import ConnectorKind, Engine
from enikk_contracts.delegation import (
    ASK_EVENT_PREFIX,
    AskCard,
    AskDecision,
    AskRequest,
    AskStatus,
    DelegateRequest,
    delegated_session_id,
)
from enikk_contracts.events import InboundEvent
from enikk_contracts.outbox import OutboundKind, OutboxPoll
from enikk_contracts.tasks import LeaseRequest, ResultReport, Succeeded, TaskAssignment, TaskStatus
from enikk_core.asks import AskService
from enikk_core.settings import TaskSettings
from enikk_core.usecases import CoreUseCases
from enikk_core.wakeups import HEADER, SessionWaker
from enikk_core_domain.errors import PermissionDeniedError
from enikk_core_domain.model import WorkerScope
from enikk_core_domain.testing import InMemoryDatabase, StaticAgentCatalog, agent, conversation
from enikk_kernel.ids import RandomIdGenerator
from enikk_kernel.testing import ManualClock, SequentialIds

pytestmark = pytest.mark.anyio

SCOPE = WorkerScope.shared(Engine.CLAUDE)
NOW = LeaseRequest(wait_seconds=0)
POLL = OutboxPoll(wait_seconds=0)
THREAD = conversation("1900.1", channel="CRITA")
OWNER = "U0OWNER"


@dataclass
class World:
    db: InMemoryDatabase
    clock: ManualClock
    usecases: CoreUseCases
    asks: AskService


@pytest.fixture
def world() -> World:
    db = InMemoryDatabase()
    clock = ManualClock()

    async def sleep(seconds: float) -> None:
        clock.advance(timedelta(seconds=seconds))

    boss = agent("boss").model_copy(update={"delegates": ("helper",)})
    helper = agent("helper").model_copy(update={"asks": ("boss",)})
    catalog = StaticAgentCatalog(boss, helper, agent("other"))
    waker = SessionWaker(catalog, clock, RandomIdGenerator(), 1)
    ids = SequentialIds()
    usecases = CoreUseCases(
        uow=db.unit_of_work,
        catalog=catalog,
        clock=clock,
        ids=ids,
        policy=TaskSettings(lease_seconds=60, max_attempts=1),
        sleep=sleep,
        waker=waker,
    )
    asks = AskService(
        uow=db.unit_of_work,
        catalog=catalog,
        clock=clock,
        ids=ids,
        owner=OWNER,
        max_attempts=1,
        waker=waker,
    )
    return World(db=db, clock=clock, usecases=usecases, asks=asks)


async def _talk(world: World, text: str = "hi", who: str = "helper") -> TaskAssignment:
    accepted = await world.usecases.accept_event(
        InboundEvent(
            event_id=f"slack:{uuid.uuid4()}", conversation=THREAD, user=OWNER, text=text, agent=who
        )
    )
    assignment = (await world.usecases.lease_task(Engine.CLAUDE, NOW)).assignment
    assert assignment is not None
    assert assignment.task_id == accepted.task_id
    return assignment


async def _finish(world: World, lease: TaskAssignment, text: str = "done") -> None:
    await world.usecases.report_result(
        SCOPE, lease.task_id, ResultReport(lease_id=lease.lease_id, outcome=Succeeded(text=text))
    )


async def _request(world: World, lease: TaskAssignment, target: str = "boss") -> uuid.UUID:
    created = await world.asks.ask(
        lease.task_id,
        AskRequest(lease_id=lease.lease_id, agent=target, request="add a tool", reason="I lack it"),
    )
    return created.request_id


async def _next(world: World) -> TaskAssignment | None:
    return (await world.usecases.lease_task(Engine.CLAUDE, NOW)).assignment


async def test_a_request_puts_a_card_in_the_thread_and_runs_nothing(world: World) -> None:
    helper = await _talk(world)
    request_id = await _request(world, helper)
    await _finish(world, helper)

    async with world.db.unit_of_work() as uow:
        cards = [n.body for n in world.db.notices.values()]
        asked = await uow.asks.get_for_update(request_id)
    assert asked is not None
    assert asked.status is AskStatus.REQUESTED
    assert asked.expires_at == asked.created_at + timedelta(hours=24)
    assert len(cards) == 1
    card = cards[0]
    assert isinstance(card, AskCard)
    assert (card.requester, card.agent, card.origin) == ("helper", "boss", THREAD)
    assert card.request == "add a tool"
    assert await _next(world) is None


async def test_allowing_runs_the_asked_agent_and_its_result_wakes_the_thread(
    world: World,
) -> None:
    helper = await _talk(world)
    request_id = await _request(world, helper)
    await _finish(world, helper)

    result = await world.asks.decide(request_id, AskDecision(approve=True, decided_by=OWNER))
    assert result.status is AskStatus.APPROVED
    assert result.changed

    boss = await _next(world)
    assert boss is not None
    assert boss.definition.name == "boss"
    assert boss.session_id == delegated_session_id(helper.session_id, "boss")
    assert boss.input.startswith("[요청] helper 가")
    assert "I lack it" in boss.input
    assert boss.input.endswith("add a tool")
    async with world.db.unit_of_work() as uow:
        task = await uow.tasks.get_for_update(boss.task_id)
    assert task is not None
    assert task.source_event_id == f"{ASK_EVENT_PREFIX}{request_id}"

    await _finish(world, boss, "도구를 만들었습니다.")
    woken = await _next(world)
    assert woken is not None
    assert woken.session_id == helper.session_id
    assert woken.input.startswith(f"{HEADER} boss 의 요청 작업 완료")
    assert "도구를 만들었습니다." in woken.input


async def test_rejecting_wakes_the_thread_and_runs_nothing(world: World) -> None:
    helper = await _talk(world)
    request_id = await _request(world, helper)
    await _finish(world, helper)

    result = await world.asks.decide(request_id, AskDecision(approve=False, decided_by=OWNER))
    assert result.status is AskStatus.REJECTED
    woken = await _next(world)
    assert woken is not None
    assert woken.session_id == helper.session_id
    assert woken.input.startswith(f"{HEADER} boss 에게 보낸 요청 소유자가 거절함")
    again = await world.asks.decide(request_id, AskDecision(approve=True, decided_by=OWNER))
    assert again.status is AskStatus.REJECTED
    assert not again.changed


async def test_only_the_owner_decides(world: World) -> None:
    helper = await _talk(world)
    request_id = await _request(world, helper)
    with pytest.raises(PermissionDeniedError):
        await world.asks.decide(request_id, AskDecision(approve=True, decided_by="USOMEONE"))


async def test_an_undecided_request_expires_and_wakes_the_thread(world: World) -> None:
    helper = await _talk(world)
    request_id = await _request(world, helper)
    await _finish(world, helper)

    assert await world.asks.expire_due() == 0
    world.clock.advance(timedelta(hours=24))
    assert await world.asks.expire_due() == 1

    woken = await _next(world)
    assert woken is not None
    assert woken.input.startswith(f"{HEADER} boss 에게 보낸 요청 기한 안에 결정되지 않아 거절됨")
    batch = await world.usecases.poll_outbox(ConnectorKind.SLACK, POLL)
    assert any("24시간 안에 결정되지 않아" in m.text for m in batch.messages)

    late = await world.asks.decide(request_id, AskDecision(approve=True, decided_by=OWNER))
    assert late.status is AskStatus.EXPIRED
    assert not late.changed


async def test_a_late_press_expires_the_request_without_running_it(world: World) -> None:
    helper = await _talk(world)
    request_id = await _request(world, helper)
    await _finish(world, helper)
    world.clock.advance(timedelta(hours=25))

    late = await world.asks.decide(request_id, AskDecision(approve=True, decided_by=OWNER))
    assert late.status is AskStatus.EXPIRED
    woken = await _next(world)
    assert woken is not None
    assert woken.session_id == helper.session_id
    assert "기한 안에 결정되지 않아" in woken.input


@pytest.mark.parametrize("target", ["other", "helper", "ghost"])
async def test_only_listed_agents_may_be_asked(world: World, target: str) -> None:
    helper = await _talk(world)
    with pytest.raises(PermissionDeniedError):
        await _request(world, helper, target)


async def test_an_agent_without_asks_may_not_ask(world: World) -> None:
    boss = await _talk(world, who="boss")
    with pytest.raises(PermissionDeniedError):
        await _request(world, boss, "helper")


async def test_a_delegated_session_does_not_ask(world: World) -> None:
    boss = await _talk(world, who="boss")
    handed = await world.usecases.delegate(
        SCOPE,
        boss.task_id,
        DelegateRequest(lease_id=boss.lease_id, agent="helper", request="draw"),
    )
    await _finish(world, boss)
    child = await _next(world)
    assert child is not None
    assert child.task_id == handed.task_id
    with pytest.raises(PermissionDeniedError):
        await _request(world, child)


async def test_cancelling_the_thread_cancels_the_asked_work(world: World) -> None:
    helper = await _talk(world)
    request_id = await _request(world, helper)
    await _finish(world, helper)
    await world.asks.decide(request_id, AskDecision(approve=True, decided_by=OWNER))
    boss = await _next(world)
    assert boss is not None

    follow = await world.usecases.accept_event(
        InboundEvent(
            event_id="slack:CRITA:1900.9",
            conversation=THREAD,
            user=OWNER,
            text="stop",
            agent="helper",
        )
    )
    result = await world.usecases.cancel_task(ConnectorKind.SLACK, follow.task_id)
    assert set(result.cancelled) == {follow.task_id, boss.task_id}
    async with world.db.unit_of_work() as uow:
        cancelled = await uow.tasks.get_for_update(boss.task_id)
    assert cancelled is not None
    assert cancelled.status is TaskStatus.CANCELLED
    batch = await world.usecases.poll_outbox(ConnectorKind.SLACK, POLL)
    assert any(
        m.task_id == boss.task_id and m.kind is OutboundKind.CANCELLED for m in batch.messages
    )
