"""Schedules through the core API (ADR-0039 S1): the scheduler role, the caller's lease,
tool limits and the owner's approval rule."""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import TYPE_CHECKING

import httpx
import pytest

from enikk_contracts import routes
from enikk_contracts.agents import ToolPolicy
from enikk_contracts.common import ConnectorKind, ConversationRef
from enikk_contracts.schedules import (
    Caller,
    RunList,
    RunStatus,
    ScheduleCreate,
    ScheduleList,
    SchedulePause,
    ScheduleQuery,
    ScheduleSpec,
    ScheduleState,
    ScheduleUpdate,
    ScheduleView,
)
from enikk_core_domain.schedules import ScheduleRun
from enikk_core_domain.testing import schedule_spec, session, task

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from conftest import Harness

    from enikk_contracts.base import Contract

pytestmark = pytest.mark.anyio


class Api:
    def __init__(self, client: httpx.AsyncClient) -> None:
        self._client = client

    async def post(self, path: str, body: Contract, as_: str = "scheduler") -> httpx.Response:
        return await self._client.post(
            path,
            content=body.model_dump_json(),
            headers={"Authorization": f"Bearer {as_}", "Content-Type": "application/json"},
        )


@pytest.fixture
async def api(harness: Harness) -> AsyncIterator[Api]:
    transport = httpx.ASGITransport(app=harness.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://core.test") as client:
        yield Api(client)


async def _caller(harness: Harness, connector: ConnectorKind = ConnectorKind.SLACK) -> Caller:
    owner = session(thread=str(uuid.uuid4()))
    if connector is not ConnectorKind.SLACK:
        where = ConversationRef(connector=connector, channel="schedule", thread="1")
        owner = owner.model_copy(update={"conversation": where})
    lease = uuid.uuid4()
    work = task(owner).grant_lease(
        lease_id=lease, now=harness.clock.now(), duration=timedelta(hours=1)
    )
    async with harness.db.unit_of_work() as uow:
        await uow.sessions.add(owner)
        await uow.tasks.add(work)
        await uow.commit()
    return Caller(task_id=work.id, lease_id=lease)


def _spec(**changes: object) -> ScheduleSpec:
    return schedule_spec(agent="assistant").model_copy(update=changes)


async def test_create_list_get_and_runs(harness: Harness, api: Api) -> None:
    caller = await _caller(harness)
    created = await api.post(
        routes.SCHEDULES, ScheduleCreate(caller=caller, name="news", spec=_spec())
    )
    assert created.status_code == 200, created.text
    view = ScheduleView.model_validate_json(created.content)
    assert view.state is ScheduleState.ACTIVE
    assert view.owner_agent == "assistant"
    assert view.origin.connector is ConnectorKind.SLACK

    listed = await api.post(routes.SCHEDULES_LIST, ScheduleQuery(caller=caller))
    assert [s.name for s in ScheduleList.model_validate_json(listed.content).schedules] == ["news"]
    got = await api.post(routes.SCHEDULE_GET.format(name="news"), ScheduleQuery(caller=caller))
    assert ScheduleView.model_validate_json(got.content) == view

    now = harness.clock.now()
    async with harness.db.unit_of_work() as uow:
        await uow.schedule_runs.add(
            ScheduleRun(
                schedule_id=view.id,
                slot=now,
                status=RunStatus.SUCCEEDED,
                started_at=now,
                updated_at=now,
            )
        )
        await uow.commit()
    runs = await api.post(routes.SCHEDULE_RUNS.format(name="news"), ScheduleQuery(caller=caller))
    assert [r.status for r in RunList.model_validate_json(runs.content).runs] == [
        RunStatus.SUCCEEDED
    ]


async def test_only_the_scheduler_role_calls(harness: Harness, api: Api) -> None:
    caller = await _caller(harness)
    for token in ("claude", "slack", "stranger"):
        response = await api.post(routes.SCHEDULES_LIST, ScheduleQuery(caller=caller), token)
        assert response.status_code == 403


async def test_the_caller_needs_a_live_lease(harness: Harness, api: Api) -> None:
    caller = await _caller(harness)
    wrong = caller.model_copy(update={"lease_id": uuid.uuid4()})
    assert (await api.post(routes.SCHEDULES_LIST, ScheduleQuery(caller=wrong))).status_code == 409
    ghost = caller.model_copy(update={"task_id": uuid.uuid4()})
    assert (await api.post(routes.SCHEDULES_LIST, ScheduleQuery(caller=ghost))).status_code == 403
    harness.clock.advance(timedelta(hours=2))
    assert (await api.post(routes.SCHEDULES_LIST, ScheduleQuery(caller=caller))).status_code == 409


async def test_scheduled_sessions_only_read(harness: Harness, api: Api) -> None:
    owner = await _caller(harness)
    await api.post(routes.SCHEDULES, ScheduleCreate(caller=owner, name="news", spec=_spec()))
    scheduled = await _caller(harness, ConnectorKind.SCHEDULE)
    assert (
        await api.post(routes.SCHEDULES_LIST, ScheduleQuery(caller=scheduled))
    ).status_code == 200
    refused = [
        await api.post(
            routes.SCHEDULES, ScheduleCreate(caller=scheduled, name="more", spec=_spec())
        ),
        await api.post(
            routes.SCHEDULE_UPDATE.format(name="news"),
            ScheduleUpdate(caller=scheduled, spec=_spec()),
        ),
        await api.post(
            routes.SCHEDULE_PAUSE.format(name="news"), SchedulePause(caller=scheduled, paused=True)
        ),
        await api.post(routes.SCHEDULE_DELETE.format(name="news"), ScheduleQuery(caller=scheduled)),
    ]
    assert [r.status_code for r in refused] == [403, 403, 403, 403]


async def test_tools_beyond_the_definition_are_refused(harness: Harness, api: Api) -> None:
    caller = await _caller(harness)
    wide = _spec(tools=ToolPolicy(builtin=("Bash",)))
    response = await api.post(routes.SCHEDULES, ScheduleCreate(caller=caller, name="x", spec=wide))
    assert response.status_code == 422
    assert "I-16" in response.text
    ghost = _spec(agent="ghost")
    response = await api.post(routes.SCHEDULES, ScheduleCreate(caller=caller, name="y", spec=ghost))
    assert response.status_code == 422


async def test_outside_the_own_range_waits_for_approval(harness: Harness, api: Api) -> None:
    caller = await _caller(harness)
    other = await api.post(
        routes.SCHEDULES, ScheduleCreate(caller=caller, name="paint", spec=_spec(agent="painter"))
    )
    assert ScheduleView.model_validate_json(other.content).state is ScheduleState.PENDING
    often = await api.post(
        routes.SCHEDULES,
        ScheduleCreate(
            caller=caller, name="often", spec=schedule_spec(agent="assistant", cron="* * * * *")
        ),
    )
    assert ScheduleView.model_validate_json(often.content).state is ScheduleState.PENDING
    paused = await api.post(
        routes.SCHEDULE_PAUSE.format(name="often"), SchedulePause(caller=caller, paused=True)
    )
    assert paused.status_code == 409


async def test_update_pause_resume_and_delete(harness: Harness, api: Api) -> None:
    caller = await _caller(harness)
    await api.post(routes.SCHEDULES, ScheduleCreate(caller=caller, name="news", spec=_spec()))
    duplicate = await api.post(
        routes.SCHEDULES, ScheduleCreate(caller=caller, name="news", spec=_spec())
    )
    assert duplicate.status_code == 409

    paused = await api.post(
        routes.SCHEDULE_PAUSE.format(name="news"), SchedulePause(caller=caller, paused=True)
    )
    assert ScheduleView.model_validate_json(paused.content).state is ScheduleState.PAUSED
    updated = await api.post(
        routes.SCHEDULE_UPDATE.format(name="news"),
        ScheduleUpdate(caller=caller, spec=_spec(description="evening news")),
    )
    view = ScheduleView.model_validate_json(updated.content)
    assert (view.spec.description, view.state) == ("evening news", ScheduleState.PAUSED)
    resumed = await api.post(
        routes.SCHEDULE_PAUSE.format(name="news"), SchedulePause(caller=caller, paused=False)
    )
    assert ScheduleView.model_validate_json(resumed.content).state is ScheduleState.ACTIVE

    deleted = await api.post(
        routes.SCHEDULE_DELETE.format(name="news"), ScheduleQuery(caller=caller)
    )
    assert deleted.status_code == 200
    missing = await api.post(routes.SCHEDULE_GET.format(name="news"), ScheduleQuery(caller=caller))
    assert missing.status_code == 404


async def test_a_session_caller_is_its_running_task(harness: Harness, api: Api) -> None:
    """ADR-0041 §3: the gateway names the session; core finds its running task."""
    by_task = await _caller(harness)
    async with harness.db.unit_of_work() as uow:
        work = await uow.tasks.get_for_update(by_task.task_id)  # type: ignore[arg-type]
    assert work is not None
    caller = Caller(session_id=work.session_id)
    listed = await api.post(routes.SCHEDULES_LIST, ScheduleQuery(caller=caller))
    assert listed.status_code == 200, listed.text
    nobody = Caller(session_id=uuid.uuid4())
    assert (await api.post(routes.SCHEDULES_LIST, ScheduleQuery(caller=nobody))).status_code == 403
    harness.clock.advance(timedelta(hours=2))
    assert (await api.post(routes.SCHEDULES_LIST, ScheduleQuery(caller=caller))).status_code == 403
