"""A thread's agent hands work to another agent and is woken by the result (ADR-0047)."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import timedelta

import pytest

from enikk_contracts.common import ConnectorKind, Engine
from enikk_contracts.delegation import DelegateRequest, delegated_session_id
from enikk_contracts.events import InboundEvent
from enikk_contracts.outbox import OutboundKind, OutboxPoll
from enikk_contracts.tasks import (
    Failed,
    LeaseRequest,
    ResultReport,
    Succeeded,
    TaskAssignment,
    TaskStatus,
)
from enikk_core.settings import TaskSettings
from enikk_core.usecases import CoreUseCases
from enikk_core.wakeups import EVENT_PREFIX, HEADER, SessionWaker
from enikk_core_domain.errors import PermissionDeniedError
from enikk_core_domain.model import WorkerScope
from enikk_core_domain.testing import InMemoryDatabase, StaticAgentCatalog, agent, conversation
from enikk_kernel.ids import RandomIdGenerator
from enikk_kernel.testing import ManualClock, SequentialIds

pytestmark = pytest.mark.anyio

SCOPE = WorkerScope.shared(Engine.CLAUDE)
NOW = LeaseRequest(wait_seconds=0)
POLL = OutboxPoll(wait_seconds=0)
THREAD = conversation("1800.1")


@dataclass
class World:
    db: InMemoryDatabase
    usecases: CoreUseCases


def _world(*, wakeups: bool = True) -> World:
    db = InMemoryDatabase()
    clock = ManualClock()

    async def sleep(seconds: float) -> None:
        clock.advance(timedelta(seconds=seconds))

    boss = agent("boss").model_copy(update={"delegates": ("helper",)})
    catalog = StaticAgentCatalog(boss, agent("helper"), agent("other"))
    usecases = CoreUseCases(
        uow=db.unit_of_work,
        catalog=catalog,
        clock=clock,
        ids=SequentialIds(),
        policy=TaskSettings(lease_seconds=60, max_attempts=1),
        sleep=sleep,
        waker=SessionWaker(catalog, clock, RandomIdGenerator(), 1) if wakeups else None,
    )
    return World(db=db, usecases=usecases)


@pytest.fixture
def world() -> World:
    return _world()


async def _ask(world: World, text: str = "hi", agent_name: str = "boss") -> TaskAssignment:
    accepted = await world.usecases.accept_event(
        InboundEvent(
            event_id=f"slack:{uuid.uuid4()}",
            conversation=THREAD,
            user="U1",
            text=text,
            agent=agent_name,
        )
    )
    return await _lease(world, accepted.task_id)


async def _lease(world: World, task_id: uuid.UUID) -> TaskAssignment:
    assignment = (await world.usecases.lease_task(Engine.CLAUDE, NOW)).assignment
    assert assignment is not None
    assert assignment.task_id == task_id
    return assignment


async def _finish(world: World, lease: TaskAssignment, text: str = "done") -> None:
    await world.usecases.report_result(
        SCOPE,
        lease.task_id,
        ResultReport(lease_id=lease.lease_id, outcome=Succeeded(text=text)),
    )


async def _hand_on(world: World, lease: TaskAssignment, agent_name: str = "helper") -> uuid.UUID:
    handed = await world.usecases.delegate(
        SCOPE,
        lease.task_id,
        DelegateRequest(lease_id=lease.lease_id, agent=agent_name, request="draw a tree"),
    )
    assert not handed.duplicate
    return handed.task_id


async def _status(world: World, task_id: uuid.UUID) -> TaskStatus:
    async with world.db.unit_of_work() as uow:
        found = await uow.tasks.get_for_update(task_id)
    assert found is not None
    return found.status


async def test_the_result_wakes_the_thread_that_handed_work_on(world: World) -> None:
    boss = await _ask(world)
    child_id = await _hand_on(world, boss)
    await _finish(world, boss, "리타에게 맡겼습니다.")

    child = await _lease(world, child_id)
    assert child.definition.name == "helper"
    assert child.session_id == delegated_session_id(boss.session_id, "helper")
    assert child.input.startswith("[위임]")
    assert child.input.endswith("draw a tree")
    await _finish(world, child, "tree.png 를 만들었습니다.")

    woken = (await world.usecases.lease_task(Engine.CLAUDE, NOW)).assignment
    assert woken is not None
    assert woken.session_id == boss.session_id
    assert woken.definition.name == "boss"
    assert woken.input.startswith(f"{HEADER} helper 의 위임 작업 완료")
    assert "tree.png 를 만들었습니다." in woken.input
    async with world.db.unit_of_work() as uow:
        task = await uow.tasks.get_for_update(woken.task_id)
    assert task is not None
    assert task.source_event_id == f"{EVENT_PREFIX}delegated:{child_id}"

    batch = await world.usecases.poll_outbox(ConnectorKind.SLACK, POLL)
    replies = [m for m in batch.messages if m.task_id == child_id and m.kind is OutboundKind.REPLY]
    assert [m.text for m in replies] == [
        "helper 가 맡은 작업을 마쳤습니다. 결과는 이 스레드에서 이어서 전합니다."
    ]
    assert all(m.conversation == THREAD for m in batch.messages)


async def test_a_failure_wakes_the_thread_too(world: World) -> None:
    boss = await _ask(world)
    child_id = await _hand_on(world, boss)
    await _finish(world, boss)
    child = await _lease(world, child_id)
    await world.usecases.report_result(
        SCOPE,
        child.task_id,
        ResultReport(lease_id=child.lease_id, outcome=Failed(reason="editor off", retryable=False)),
    )
    woken = (await world.usecases.lease_task(Engine.CLAUDE, NOW)).assignment
    assert woken is not None
    assert woken.input.startswith(f"{HEADER} helper 의 위임 작업 실패")
    assert "editor off" in woken.input


async def test_delegating_again_continues_the_same_session(world: World) -> None:
    first = await _ask(world)
    child_id = await _hand_on(world, first)
    again = await world.usecases.delegate(
        SCOPE,
        first.task_id,
        DelegateRequest(lease_id=first.lease_id, agent="helper", request="another"),
    )
    assert again.duplicate
    assert again.task_id == child_id
    await _finish(world, first)
    await _finish(world, await _lease(world, child_id))
    woken = (await world.usecases.lease_task(Engine.CLAUDE, NOW)).assignment
    assert woken is not None
    second_child = await _hand_on(world, woken)
    async with world.db.unit_of_work() as uow:
        one = await uow.tasks.get_for_update(child_id)
        two = await uow.tasks.get_for_update(second_child)
    assert one is not None
    assert two is not None
    assert one.session_id == two.session_id


@pytest.mark.parametrize("target", ["other", "boss", "ghost"])
async def test_only_listed_agents_take_work(world: World, target: str) -> None:
    boss = await _ask(world)
    with pytest.raises(PermissionDeniedError):
        await world.usecases.delegate(
            SCOPE,
            boss.task_id,
            DelegateRequest(lease_id=boss.lease_id, agent=target, request="x"),
        )


async def test_a_delegated_agent_does_not_hand_work_on(world: World) -> None:
    boss = await _ask(world)
    child_id = await _hand_on(world, boss)
    await _finish(world, boss)
    child = await _lease(world, child_id)
    with pytest.raises(PermissionDeniedError):
        await world.usecases.delegate(
            SCOPE,
            child.task_id,
            DelegateRequest(lease_id=child.lease_id, agent="helper", request="x"),
        )


async def test_cancelling_the_thread_cancels_the_handed_work(world: World) -> None:
    boss = await _ask(world)
    child_id = await _hand_on(world, boss)
    await _finish(world, boss)
    await _lease(world, child_id)
    follow = await world.usecases.accept_event(
        InboundEvent(
            event_id="slack:C1:1800.9", conversation=THREAD, user="U1", text="more", agent="boss"
        )
    )
    result = await world.usecases.cancel_task(ConnectorKind.SLACK, follow.task_id)
    assert set(result.cancelled) == {follow.task_id, child_id}
    assert await _status(world, child_id) is TaskStatus.CANCELLED
    batch = await world.usecases.poll_outbox(ConnectorKind.SLACK, POLL)
    cancelled = [m for m in batch.messages if m.kind is OutboundKind.CANCELLED]
    assert {m.task_id for m in cancelled} == {follow.task_id, child_id}
    assert (await world.usecases.lease_task(Engine.CLAUDE, NOW)).assignment is None


async def test_without_wake_ups_the_result_is_posted_in_the_thread() -> None:
    world = _world(wakeups=False)
    boss = await _ask(world)
    child_id = await _hand_on(world, boss)
    await _finish(world, boss)
    await _finish(world, await _lease(world, child_id), "tree.png")
    batch = await world.usecases.poll_outbox(ConnectorKind.SLACK, POLL)
    replies = [m for m in batch.messages if m.task_id == child_id and m.kind is OutboundKind.REPLY]
    assert [m.text for m in replies] == ["tree.png"]
    assert (await world.usecases.lease_task(Engine.CLAUDE, NOW)).assignment is None
