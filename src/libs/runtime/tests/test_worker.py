from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from enikk_contracts.agents import AgentDefinition, Execution, ToolPolicy
from enikk_contracts.common import Engine
from enikk_contracts.tasks import Failed, Succeeded
from enikk_core_client.errors import CoreUnavailableError
from enikk_kernel.testing import ManualClock
from enikk_runtime.errors import EngineError
from enikk_runtime.model import EngineResult, SessionBundle, SessionWorkspace
from enikk_runtime.testing import (
    FakeCore,
    RecordingObserver,
    ScriptedEngine,
    StaticPromptStore,
    definition,
    prompt,
)
from enikk_runtime.worker import INTERNAL_FAILURE, TaskWorker, WorkerPolicy

if TYPE_CHECKING:
    import uuid
    from collections.abc import Awaitable, Callable

    from enikk_runtime.model import EngineJob
    from enikk_runtime.ports import Workspaces

pytestmark = pytest.mark.anyio


async def _tick(_: float) -> None:
    await asyncio.sleep(0.001)


def _worker(
    core: FakeCore,
    engine: ScriptedEngine | None = None,
    *,
    store: StaticPromptStore | None = None,
    policy: WorkerPolicy | None = None,
    sleep: Callable[[float], Awaitable[None]] = _tick,
    workspaces: Workspaces | None = None,
) -> TaskWorker:
    return TaskWorker(
        core=core,
        prompts=store or StaticPromptStore(prompt()),
        engine=engine or ScriptedEngine(),
        clock=ManualClock(),
        policy=policy or WorkerPolicy(),
        sleep=sleep,
        workspaces=workspaces,
    )


def _outcome(core: FakeCore) -> Succeeded | Failed:
    [(_, report)] = core.reports
    return report.outcome


async def test_runs_the_engine_and_reports_success() -> None:
    core = FakeCore(ManualClock())
    task = core.assign(text="hi there")
    engine = ScriptedEngine()
    assert await _worker(core, engine).run_once()

    [job] = engine.jobs
    assert job.task_id == task.task_id
    assert job.lease_id == task.lease_id
    assert job.prompt.version == 3
    assert job.definition.name == "assistant"
    outcome = _outcome(core)
    assert isinstance(outcome, Succeeded)
    assert outcome.text == "echo: hi there"
    assert outcome.engine_session_id == "engine-session-1"


async def test_no_task_means_nothing_to_report() -> None:
    core = FakeCore(ManualClock())
    assert not await _worker(core).run_once()
    assert core.reports == []


async def test_missing_prompt_is_a_retryable_failure_without_running() -> None:
    core = FakeCore(ManualClock())
    core.assign()
    engine = ScriptedEngine()
    await _worker(core, engine, store=StaticPromptStore()).run_once()
    assert engine.jobs == []
    outcome = _outcome(core)
    assert isinstance(outcome, Failed)
    assert outcome.retryable
    assert "assistant" in outcome.reason


async def test_model_of_another_engine_is_refused() -> None:
    core = FakeCore(ManualClock())
    core.assign()
    engine = ScriptedEngine()
    store = StaticPromptStore(prompt(model="gemini-9-pro"))
    await _worker(core, engine, store=store).run_once()
    assert engine.jobs == []
    outcome = _outcome(core)
    assert isinstance(outcome, Failed)
    assert not outcome.retryable
    assert "gemini-9-pro" in outcome.reason


async def test_agent_of_another_engine_is_refused() -> None:
    core = FakeCore(ManualClock())
    core.assign(definition("painter", Engine.GEMINI))
    await _worker(core).run_once()
    outcome = _outcome(core)
    assert isinstance(outcome, Failed)
    assert not outcome.retryable


async def test_engine_failure_keeps_its_retry_decision() -> None:
    async def flaky(_: EngineJob) -> EngineResult:
        raise EngineError("rate limited", retryable=True)

    core = FakeCore(ManualClock())
    core.assign()
    await _worker(core, ScriptedEngine(flaky)).run_once()
    outcome = _outcome(core)
    assert isinstance(outcome, Failed)
    assert outcome.retryable
    assert outcome.reason == "rate limited"


async def test_unexpected_error_is_reported_without_its_details() -> None:
    async def broken(_: EngineJob) -> EngineResult:
        raise RuntimeError("token=xoxb-should-not-leak")

    core = FakeCore(ManualClock())
    core.assign()
    await _worker(core, ScriptedEngine(broken)).run_once()
    outcome = _outcome(core)
    assert isinstance(outcome, Failed)
    assert outcome.reason == INTERNAL_FAILURE
    assert not outcome.retryable


async def test_run_longer_than_the_limit_times_out() -> None:
    async def forever(_: EngineJob) -> EngineResult:
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    core = FakeCore(ManualClock())
    core.assign()
    policy = WorkerPolicy(timeout_cap_seconds=0.05)
    await _worker(core, ScriptedEngine(forever), policy=policy).run_once()
    outcome = _outcome(core)
    assert isinstance(outcome, Failed)
    assert "did not finish" in outcome.reason


async def test_heartbeats_keep_the_lease_while_running() -> None:
    core = FakeCore(ManualClock())
    core.assign()

    async def slow(job: EngineJob) -> EngineResult:
        while len(core.heartbeats) < 3:
            await asyncio.sleep(0.001)
        return EngineResult(text="done")

    await _worker(core, ScriptedEngine(slow)).run_once()
    assert len(core.heartbeats) >= 3
    assert isinstance(_outcome(core), Succeeded)


async def test_lost_lease_cancels_the_run_and_reports_nothing() -> None:
    core = FakeCore(ManualClock())
    task = core.assign()
    cancelled = asyncio.Event()

    async def forever(_: EngineJob) -> EngineResult:
        core.lost_leases.add(task.lease_id)
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise
        raise AssertionError("unreachable")

    assert await _worker(core, ScriptedEngine(forever)).run_once()
    assert cancelled.is_set()
    assert core.reports == []


async def test_report_is_retried_while_core_is_down() -> None:
    core = FakeCore(ManualClock())
    core.assign()

    async def then_outage(job: EngineJob) -> EngineResult:
        core.outages = 2
        return EngineResult(text="ok")

    await _worker(core, ScriptedEngine(then_outage)).run_once()
    assert isinstance(_outcome(core), Succeeded)


async def test_report_gives_up_after_its_attempts() -> None:
    core = FakeCore(ManualClock())
    core.assign()

    async def then_long_outage(job: EngineJob) -> EngineResult:
        core.outages = 10
        return EngineResult(text="ok")

    worker = _worker(core, ScriptedEngine(then_long_outage), policy=WorkerPolicy(report_attempts=2))
    with pytest.raises(CoreUnavailableError):
        await worker.run_once()


async def test_run_forever_backs_off_while_core_is_down_and_stops() -> None:
    core = FakeCore(ManualClock())
    core.outages = 3
    pauses: list[float] = []
    worker: TaskWorker

    async def record(seconds: float) -> None:
        pauses.append(seconds)
        if len(pauses) == 3:
            worker.stop()
        await asyncio.sleep(0)

    worker = _worker(core, sleep=record)
    await worker.run_forever()
    assert pauses == [1.0, 2.0, 4.0]


async def test_two_slots_run_two_tasks_at_the_same_time() -> None:
    core = FakeCore(ManualClock())
    first, second = core.assign(text="one"), core.assign(text="two")
    running: set[uuid.UUID] = set()
    both = asyncio.Event()
    worker: TaskWorker

    async def respond(job: EngineJob) -> EngineResult:
        running.add(job.task_id)
        if len(running) == 2:
            both.set()
            worker.stop()
        await asyncio.wait_for(both.wait(), timeout=5)
        return EngineResult(text=f"done: {job.input}", engine_session_id=None)

    worker = _worker(core, ScriptedEngine(respond), policy=WorkerPolicy(slots=2))
    await asyncio.wait_for(worker.run_forever(), timeout=5)

    assert running == {first.task_id, second.task_id}
    assert {task_id for task_id, _ in core.reports} == {first.task_id, second.task_id}
    assert all(isinstance(r.outcome, Succeeded) for _, r in core.reports)


async def test_stop_lets_every_slot_finish_its_task_and_lease_no_more() -> None:
    core = FakeCore(ManualClock())
    running = [core.assign(text="one"), core.assign(text="two")]
    waiting = core.assign(text="three")
    started = 0
    release = asyncio.Event()

    async def respond(job: EngineJob) -> EngineResult:
        nonlocal started
        started += 1
        await release.wait()
        return EngineResult(text=f"done: {job.input}", engine_session_id=None)

    worker = _worker(core, ScriptedEngine(respond), policy=WorkerPolicy(slots=2))
    loop = asyncio.create_task(worker.run_forever())
    while started < 2:
        await asyncio.sleep(0.001)
    worker.stop()
    release.set()
    await asyncio.wait_for(loop, timeout=5)

    assert {task_id for task_id, _ in core.reports} == {t.task_id for t in running}
    assert core.queue == [waiting]


def test_slots_are_between_one_and_four() -> None:
    assert WorkerPolicy().slots == 1
    with pytest.raises(ValueError, match="slots"):
        WorkerPolicy(slots=0)
    with pytest.raises(ValueError, match="slots"):
        WorkerPolicy(slots=5)


async def test_observer_records_success_and_failure() -> None:
    core = FakeCore(ManualClock())
    first = core.assign(text="one")
    observer = RecordingObserver()

    async def second_fails(job: EngineJob) -> EngineResult:
        if job.input == "two":
            raise EngineError("busy", retryable=True)
        return EngineResult(text="done")

    worker = TaskWorker(
        core=core,
        prompts=StaticPromptStore(prompt()),
        engine=ScriptedEngine(second_fails),
        clock=ManualClock(),
        policy=WorkerPolicy(),
        observer=observer,
        sleep=_tick,
    )
    await worker.run_once()
    core.assign(text="two")
    await worker.run_once()
    assert observer.events == [
        ("started", str(first.task_id)),
        ("succeeded", "done"),
        observer.events[2],
        ("failed", "busy"),
    ]


class _Workspaces:
    def __init__(self, *, broken: bool = False) -> None:
        self.sessions: list[uuid.UUID] = []
        self._broken = broken

    def prepare(self, session_id: uuid.UUID) -> SessionWorkspace:
        self.sessions.append(session_id)
        if self._broken:
            raise OSError("volume not mounted")
        return SessionWorkspace(
            repository="404Vector/project-enikk",
            repo_dir=Path("/data/repo.git"),
            snapshot_dir=Path("/data/snapshots") / ("b" * 40),
            commit="b" * 40,
            fetched_at=None,
            fresh=True,
            worktree_dir=Path("/data/worktrees") / str(session_id),
            branch=f"session/{session_id}",
            tmp_dir=Path("/data/tmp") / str(session_id),
        )

    def bundle(self, session_id: uuid.UUID) -> SessionBundle:
        raise AssertionError("the worker never bundles")


def _with_tools(execution: Execution = Execution.SHARED) -> AgentDefinition:
    return definition().model_copy(
        update={"tools": ToolPolicy(builtin=("Read", "Bash")), "execution": execution}
    )


async def test_a_shared_role_with_tools_gets_its_session_workspace() -> None:
    core = FakeCore(ManualClock())
    task = core.assign(_with_tools(), text="리뷰해 줘")
    engine = ScriptedEngine()
    workspaces = _Workspaces()
    await _worker(core, engine, workspaces=workspaces).run_once()

    assert workspaces.sessions == [task.session_id]
    [job] = engine.jobs
    assert job.workspace is not None
    assert job.workspace.worktree_dir == Path("/data/worktrees") / str(task.session_id)
    assert "b" * 40 in job.input
    assert job.input.endswith("리뷰해 줘")


@pytest.mark.parametrize(
    "agent",
    [definition(), _with_tools(Execution.ISOLATED)],
    ids=["no-tools", "isolated"],
)
async def test_other_roles_get_no_workspace(agent: AgentDefinition) -> None:
    core = FakeCore(ManualClock())
    core.assign(agent, text="hi")
    engine = ScriptedEngine()
    workspaces = _Workspaces()
    await _worker(core, engine, workspaces=workspaces).run_once()

    assert workspaces.sessions == []
    [job] = engine.jobs
    assert job.workspace is None
    assert job.input == "hi"


async def test_a_broken_volume_does_not_fail_the_task() -> None:
    core = FakeCore(ManualClock())
    core.assign(_with_tools(), text="hi")
    engine = ScriptedEngine()
    await _worker(core, engine, workspaces=_Workspaces(broken=True)).run_once()

    [job] = engine.jobs
    assert job.workspace is None
    assert job.input == "hi"
    assert isinstance(_outcome(core), Succeeded)
