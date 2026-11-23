"""The lease → run → report loop every engine runtime shares."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import TYPE_CHECKING, Final

from pydantic import Field

from enikk_contracts.agents import Execution
from enikk_contracts.tasks import Failed, Heartbeat, LeaseRequest, ResultReport, Succeeded
from enikk_core_client.errors import CoreConflictError, CoreNotFoundError, CoreUnavailableError
from enikk_kernel.models import FrozenModel
from enikk_runtime.errors import IncompatibleModelError, TaskFailedError
from enikk_runtime.memory import with_memories
from enikk_runtime.model import EngineJob
from enikk_runtime.observing import NullObserver
from enikk_runtime.skills import uses_skills, with_skills
from enikk_runtime.workspace import with_workspace

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from enikk_contracts.skills import SkillSummary
    from enikk_contracts.tasks import TaskAssignment
    from enikk_kernel.clock import Clock
    from enikk_runtime.model import EngineResult, SessionWorkspace
    from enikk_runtime.ports import (
        ModelEngine,
        PromptStore,
        RunObserver,
        SkillLibrary,
        TaskSource,
        Workspaces,
    )

    Sleep = Callable[[float], Awaitable[None]]

MAX_REASON_LENGTH: Final = 2_000
INTERNAL_FAILURE: Final = "internal error while running the agent; see the runtime logs"

_log = logging.getLogger(__name__)


class WorkerPolicy(FrozenModel):
    wait_seconds: int = Field(default=20, ge=0, le=25)
    backoff_initial_seconds: float = Field(default=1.0, gt=0)
    backoff_max_seconds: float = Field(default=30.0, gt=0)
    min_heartbeat_seconds: float = Field(default=1.0, gt=0)
    report_attempts: int = Field(default=3, ge=1, le=10)
    timeout_cap_seconds: float | None = Field(default=None, gt=0)
    """Upper bound on every run, on top of the agent's own ``timeout_seconds``."""


class TaskWorker:
    """Leases one task at a time from core and runs it on ``engine``."""

    def __init__(
        self,
        *,
        core: TaskSource,
        prompts: PromptStore,
        engine: ModelEngine,
        clock: Clock,
        policy: WorkerPolicy,
        observer: RunObserver | None = None,
        workspaces: Workspaces | None = None,
        skills: SkillLibrary | None = None,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        self._core = core
        self._prompts = prompts
        self._engine = engine
        self._observer: RunObserver = observer or NullObserver()
        self._workspaces = workspaces
        self._skills = skills
        self._clock = clock
        self._policy = policy
        self._sleep = sleep
        self._stopping = asyncio.Event()

    def stop(self) -> None:
        """Finish the current task, then stop leasing."""
        self._stopping.set()

    async def run_forever(self) -> None:
        backoff = self._policy.backoff_initial_seconds
        while not self._stopping.is_set():
            try:
                await self.run_once()
            except CoreUnavailableError as exc:
                _log.warning("core unavailable, retrying in %.0fs: %s", backoff, exc)
                await self._sleep(backoff)
                backoff = min(backoff * 2, self._policy.backoff_max_seconds)
            else:
                backoff = self._policy.backoff_initial_seconds
        _log.info("worker stopped")

    async def run_once(self) -> bool:
        """Lease and handle at most one task. Returns whether a task was handled."""
        request = LeaseRequest(wait_seconds=self._policy.wait_seconds)
        assignment = (await self._core.lease_task(request)).assignment
        if assignment is None:
            return False
        _log.info(
            "task %s leased (agent %s, attempt %d)",
            assignment.task_id,
            assignment.definition.name,
            assignment.attempt,
        )
        await self.run_assignment(assignment)
        return True

    async def run_assignment(self, assignment: TaskAssignment) -> Succeeded | Failed | None:
        """Run an already leased task and report it. ``None`` when the lease was lost."""
        outcome = await self._run_while_leased(assignment)
        if outcome is None:
            _log.warning("task %s: lease lost, result discarded", assignment.task_id)
            return None
        await self._report(assignment, outcome)
        return outcome

    # ------------------------------------------------------------ lease keeping

    async def _run_while_leased(self, assignment: TaskAssignment) -> Succeeded | Failed | None:
        run = asyncio.create_task(self._execute(assignment))
        beat = asyncio.create_task(self._keep_lease(assignment))
        try:
            await asyncio.wait({run, beat}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in (run, beat):
                if not task.done():
                    task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await task
        if run.done() and not run.cancelled():
            return run.result()
        return None

    async def _keep_lease(self, assignment: TaskAssignment) -> None:
        expires_at = assignment.lease_expires_at
        while True:
            remaining = (expires_at - self._clock.now()).total_seconds()
            await self._sleep(max(remaining / 3, self._policy.min_heartbeat_seconds))
            try:
                extended = await self._core.heartbeat(
                    assignment.task_id, Heartbeat(lease_id=assignment.lease_id)
                )
            except (CoreConflictError, CoreNotFoundError):
                return
            except CoreUnavailableError as exc:
                _log.warning("task %s: heartbeat failed, will retry: %s", assignment.task_id, exc)
                continue
            expires_at = extended.lease_expires_at

    # ------------------------------------------------------------ execution

    async def _execute(self, assignment: TaskAssignment) -> Succeeded | Failed:
        timeout = float(assignment.definition.limits.timeout_seconds)
        if self._policy.timeout_cap_seconds is not None:
            timeout = min(timeout, self._policy.timeout_cap_seconds)
        try:
            async with asyncio.timeout(timeout):
                result = await self._run_engine(assignment)
        except TimeoutError:
            _log.warning("task %s timed out after %.0fs", assignment.task_id, timeout)
            return Failed(reason=f"the agent did not finish within {timeout:.0f}s", retryable=False)
        except TaskFailedError as failure:
            _log.warning(
                "task %s failed (retryable=%s): %s",
                assignment.task_id,
                failure.retryable,
                failure.reason,
            )
            return Failed(reason=failure.reason[:MAX_REASON_LENGTH], retryable=failure.retryable)
        except Exception:
            _log.exception("task %s: unexpected error", assignment.task_id)
            return Failed(reason=INTERNAL_FAILURE, retryable=False)
        return Succeeded(text=result.text, engine_session_id=result.engine_session_id)

    async def _run_engine(self, assignment: TaskAssignment) -> EngineResult:
        definition = assignment.definition
        if definition.engine is not self._engine.engine:
            raise IncompatibleModelError(
                f"agent '{definition.name}' needs engine {definition.engine}, "
                f"this runtime runs {self._engine.engine}"
            )
        prompt = await self._prompts.get(definition.prompt)
        if not self._engine.accepts_model(prompt.model):
            raise IncompatibleModelError(
                f"prompt '{prompt.name}' v{prompt.version} selects model '{prompt.model}', "
                f"which engine {self._engine.engine} cannot run"
            )
        _log.info(
            "task %s: prompt %s v%d, model %s",
            assignment.task_id,
            prompt.name,
            prompt.version,
            prompt.model,
        )
        workspace = await self._workspace(assignment)
        skills = await self._skill_list(assignment)
        text = with_workspace(with_memories(assignment.input, assignment.memories), workspace)
        job = EngineJob(
            task_id=assignment.task_id,
            lease_id=assignment.lease_id,
            attempt=assignment.attempt,
            session_id=assignment.session_id,
            engine_session_id=assignment.engine_session_id,
            definition=definition,
            prompt=prompt,
            input=with_skills(text, skills),
            workspace=workspace,
            skills=skills,
        )
        async with self._observer.observe(job) as observation:
            try:
                result = await self._engine.run(job)
            except TaskFailedError as failure:
                observation.failed(failure.reason)
                raise
            except Exception:
                observation.failed(INTERNAL_FAILURE)
                raise
            observation.succeeded(result)
        return result

    async def _workspace(self, assignment: TaskAssignment) -> SessionWorkspace | None:
        """The session's shared-volume paths, for shared roles with built-in tools only.
        The task goes on without them when the volume cannot be prepared."""
        definition = assignment.definition
        if (
            self._workspaces is None
            or not definition.tools.builtin
            or definition.execution is not Execution.SHARED
        ):
            return None
        try:
            return await asyncio.to_thread(self._workspaces.prepare, assignment.session_id)
        except Exception:
            _log.exception("task %s: shared volume unavailable", assignment.task_id)
            return None

    async def _skill_list(self, assignment: TaskAssignment) -> tuple[SkillSummary, ...] | None:
        """The skills the session may load (ADR-0037 §3); ``None`` for a role without
        them. An empty list when the store cannot list them: ``load_skill`` still works."""
        if self._skills is None or not uses_skills(assignment.definition):
            return None
        try:
            return await self._skills.catalog()
        except Exception:
            _log.exception("task %s: could not list the skills", assignment.task_id)
            return ()

    # ------------------------------------------------------------ reporting

    async def _report(self, assignment: TaskAssignment, outcome: Succeeded | Failed) -> None:
        report = ResultReport(lease_id=assignment.lease_id, outcome=outcome)
        for attempt in range(1, self._policy.report_attempts + 1):
            try:
                accepted = await self._core.report_result(assignment.task_id, report)
            except (CoreConflictError, CoreNotFoundError) as exc:
                _log.warning("task %s: result rejected, lease lost: %s", assignment.task_id, exc)
                return
            except CoreUnavailableError:
                if attempt == self._policy.report_attempts:
                    raise
                await self._sleep(self._policy.backoff_initial_seconds * attempt)
                continue
            _log.info("task %s reported %s", assignment.task_id, accepted.status)
            return
