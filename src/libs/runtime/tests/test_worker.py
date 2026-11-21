from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import pytest

from enikk_contracts.common import Engine
from enikk_contracts.tasks import Failed, Succeeded
from enikk_core_client.errors import CoreUnavailableError
from enikk_kernel.testing import ManualClock
from enikk_runtime.errors import EngineError
from enikk_runtime.model import EngineResult
from enikk_runtime.testing import FakeCore, ScriptedEngine, StaticPromptStore, definition, prompt
from enikk_runtime.worker import INTERNAL_FAILURE, TaskWorker, WorkerPolicy

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from enikk_runtime.model import EngineJob

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
) -> TaskWorker:
    return TaskWorker(
        core=core,
        prompts=store or StaticPromptStore(prompt()),
        engine=engine or ScriptedEngine(),
        clock=ManualClock(),
        policy=policy or WorkerPolicy(),
        sleep=sleep,
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
