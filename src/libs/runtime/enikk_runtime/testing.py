"""Test doubles for the runtime ports, reused by engine runtime tests."""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from datetime import timedelta
from typing import TYPE_CHECKING

from enikk_contracts.agents import AgentDefinition, PromptRef
from enikk_contracts.common import Engine
from enikk_contracts.tasks import (
    LeaseExtended,
    LeaseResponse,
    ResultAccepted,
    Succeeded,
    TaskAssignment,
    TaskStatus,
)
from enikk_core_client.errors import CoreConflictError, CoreNotFoundError, CoreUnavailableError
from enikk_runtime.errors import PromptUnavailableError
from enikk_runtime.model import EngineResult, ResolvedPrompt

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable

    from enikk_contracts.attachments import AttachmentContent, AttachmentFetch
    from enikk_contracts.tasks import Heartbeat, LeaseRequest, ResultReport
    from enikk_kernel.clock import Clock
    from enikk_runtime.model import EngineJob


def definition(name: str = "assistant", engine: Engine = Engine.CLAUDE) -> AgentDefinition:
    return AgentDefinition(
        name=name,
        description=f"{name} for tests",
        engine=engine,
        prompt=PromptRef(name=name, label="production"),
    )


def prompt(name: str = "assistant", model: str = "claude-test-1") -> ResolvedPrompt:
    return ResolvedPrompt(name=name, version=3, text=f"You are {name}.", model=model)


class FakeCore:
    """In-memory ``TaskSource`` with scriptable outages and lost leases."""

    def __init__(self, clock: Clock, lease: timedelta = timedelta(seconds=60)) -> None:
        self._clock = clock
        self._lease = lease
        self.queue: list[TaskAssignment] = []
        self.heartbeats: list[uuid.UUID] = []
        self.reports: list[tuple[uuid.UUID, ResultReport]] = []
        self.lost_leases: set[uuid.UUID] = set()
        self.outages = 0
        """Number of upcoming calls that fail with ``CoreUnavailableError``."""
        self.attachments: dict[tuple[uuid.UUID, int], list[AttachmentContent]] = {}
        """Answers to successive fetches of one attachment; the last one repeats."""

    async def fetch_attachment(
        self, task_id: uuid.UUID, index: int, fetch: AttachmentFetch
    ) -> AttachmentContent:
        answers = self.attachments.get((task_id, index))
        if not answers:
            raise CoreNotFoundError("no attachment (test)")
        return answers.pop(0) if len(answers) > 1 else answers[0]

    def assign(
        self, agent: AgentDefinition | None = None, *, text: str = "hello"
    ) -> TaskAssignment:
        assignment = TaskAssignment(
            task_id=uuid.uuid4(),
            lease_id=uuid.uuid4(),
            lease_expires_at=self._clock.now() + self._lease,
            attempt=1,
            definition=agent or definition(),
            session_id=uuid.uuid4(),
            engine_session_id=None,
            input=text,
        )
        self.queue.append(assignment)
        return assignment

    def _maybe_fail(self) -> None:
        if self.outages:
            self.outages -= 1
            raise CoreUnavailableError("core is down (test)")

    async def lease_task(self, request: LeaseRequest) -> LeaseResponse:
        self._maybe_fail()
        return LeaseResponse(assignment=self.queue.pop(0) if self.queue else None)

    async def heartbeat(self, task_id: uuid.UUID, beat: Heartbeat) -> LeaseExtended:
        self._maybe_fail()
        if beat.lease_id in self.lost_leases:
            raise CoreConflictError("lease lost (test)", status=409)
        self.heartbeats.append(task_id)
        return LeaseExtended(lease_expires_at=self._clock.now() + self._lease)

    async def report_result(self, task_id: uuid.UUID, report: ResultReport) -> ResultAccepted:
        self._maybe_fail()
        if report.lease_id in self.lost_leases:
            raise CoreConflictError("lease lost (test)", status=409)
        self.reports.append((task_id, report))
        succeeded = isinstance(report.outcome, Succeeded)
        status = TaskStatus.SUCCEEDED if succeeded else TaskStatus.FAILED
        return ResultAccepted(task_id=task_id, status=status, duplicate=False)


class StaticPromptStore:
    def __init__(self, *prompts: ResolvedPrompt) -> None:
        self._by_name = {p.name: p for p in prompts}

    async def get(self, ref: PromptRef) -> ResolvedPrompt:
        found = self._by_name.get(ref.name)
        if found is None:
            raise PromptUnavailableError(f"prompt '{ref.name}' is not available")
        return found


async def _echo(job: EngineJob) -> EngineResult:
    return EngineResult(text=f"echo: {job.input}", engine_session_id="engine-session-1")


class ScriptedEngine:
    """``ModelEngine`` that accepts ``claude-*`` models and runs ``respond``."""

    def __init__(
        self,
        respond: Callable[[EngineJob], Awaitable[EngineResult]] = _echo,
        engine: Engine = Engine.CLAUDE,
    ) -> None:
        self._respond = respond
        self._engine = engine
        self.jobs: list[EngineJob] = []

    @property
    def engine(self) -> Engine:
        return self._engine

    def accepts_model(self, model: str) -> bool:
        return model.startswith(f"{self._engine.value}-")

    async def run(self, job: EngineJob) -> EngineResult:
        self.jobs.append(job)
        return await self._respond(job)


class _Recorded:
    def __init__(self, events: list[tuple[str, str]]) -> None:
        self._events = events

    def succeeded(self, result: EngineResult) -> None:
        self._events.append(("succeeded", result.text))

    def failed(self, reason: str) -> None:
        self._events.append(("failed", reason))


class RecordingObserver:
    def __init__(self) -> None:
        self.events: list[tuple[str, str]] = []

    @asynccontextmanager
    async def observe(self, job: EngineJob) -> AsyncIterator[_Recorded]:
        self.events.append(("started", str(job.task_id)))
        yield _Recorded(self.events)
