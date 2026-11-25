"""Ports of the runtime framework. Engines and prompt stores live in adapter packages."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    import uuid
    from contextlib import AbstractAsyncContextManager

    from enikk_contracts.agents import PromptRef
    from enikk_contracts.common import Engine
    from enikk_contracts.diagnostics import (
        Deployments,
        DeploymentsQuery,
        LogLines,
        LogsQuery,
        Pulls,
        PullsQuery,
        RecentTasks,
        RecentTasksQuery,
        TaskDetail,
        TaskDetailQuery,
    )
    from enikk_contracts.memory import (
        ForgetRequest,
        MemoryMatches,
        MemorySearch,
        MemoryWritten,
        RememberRequest,
        UpdateMemoryRequest,
    )
    from enikk_contracts.prompts import (
        PromptCandidateCreated,
        PromptCandidateRequest,
        PromptRead,
        PromptReadRequest,
    )
    from enikk_contracts.skills import SkillSummary
    from enikk_contracts.tasks import (
        Heartbeat,
        LeaseExtended,
        LeaseRequest,
        LeaseResponse,
        ResultAccepted,
        ResultReport,
    )
    from enikk_runtime.model import (
        EngineJob,
        EngineResult,
        LoadedSkill,
        ResolvedPrompt,
        SessionWorkspace,
    )


class TaskSource(Protocol):
    """The task half of the core API. ``CoreClient`` implements it."""

    async def lease_task(self, request: LeaseRequest) -> LeaseResponse: ...
    async def heartbeat(self, task_id: uuid.UUID, beat: Heartbeat) -> LeaseExtended: ...
    async def report_result(self, task_id: uuid.UUID, report: ResultReport) -> ResultAccepted: ...


class Workspaces(Protocol):
    """A session's place on the shared volume (ADR-0037 §7). Blocking; may raise."""

    def prepare(self, session_id: uuid.UUID) -> SessionWorkspace: ...


class MemoryKeeper(Protocol):
    """Enikk's memory through core (ADR-0028). ``CoreClient`` implements it."""

    async def remember(self, task_id: uuid.UUID, request: RememberRequest) -> MemoryWritten: ...
    async def update_memory(
        self, task_id: uuid.UUID, request: UpdateMemoryRequest
    ) -> MemoryWritten: ...
    async def forget_memory(self, task_id: uuid.UUID, request: ForgetRequest) -> MemoryWritten: ...
    async def search_memory(self, task_id: uuid.UUID, request: MemorySearch) -> MemoryMatches: ...


class PromptProposer(Protocol):
    """Prompt candidates through core (ADR-0032). ``CoreClient`` implements it.

    Core creates the candidate and asks the owner; nothing here can change ``production``.
    """

    async def propose_prompt(
        self, task_id: uuid.UUID, request: PromptCandidateRequest
    ) -> PromptCandidateCreated: ...

    async def read_prompt(self, task_id: uuid.UUID, request: PromptReadRequest) -> PromptRead:
        """Read-only: one version of a role's prompt (ADR-0032 §8)."""
        ...


class Diagnostician(Protocol):
    """Read-only diagnostics through core (ADR-0029). ``CoreClient`` implements it."""

    async def diagnose_recent_tasks(
        self, task_id: uuid.UUID, query: RecentTasksQuery
    ) -> RecentTasks: ...
    async def diagnose_task_detail(
        self, task_id: uuid.UUID, query: TaskDetailQuery
    ) -> TaskDetail: ...
    async def diagnose_deployments(
        self, task_id: uuid.UUID, query: DeploymentsQuery
    ) -> Deployments: ...
    async def diagnose_logs(self, task_id: uuid.UUID, query: LogsQuery) -> LogLines: ...
    async def diagnose_pulls(self, task_id: uuid.UUID, query: PullsQuery) -> Pulls: ...


class PromptStore(Protocol):
    async def get(self, ref: PromptRef) -> ResolvedPrompt:
        """Fetch a prompt. Raises ``PromptUnavailableError`` instead of falling back (I-13)."""
        ...


class SkillLibrary(Protocol):
    """Skill prompts (``skill-*``) in the prompt store (ADR-0037 §2, §3)."""

    async def catalog(self) -> tuple[SkillSummary, ...]:
        """Name and description of every usable skill's production version."""
        ...

    async def load(self, name: str) -> LoadedSkill:
        """The production version of skill ``name``. Raises ``PromptInvalidError`` when it
        does not exist or is not a valid skill, ``PromptUnavailableError`` when the store
        cannot be reached."""
        ...


class ModelEngine(Protocol):
    @property
    def engine(self) -> Engine: ...

    def accepts_model(self, model: str) -> bool:
        """Whether ``model`` can run on this engine (I-16)."""
        ...

    async def run(self, job: EngineJob) -> EngineResult:
        """Run one task. Raises ``EngineError`` for failures the owner should see."""
        ...


class RunObservation(Protocol):
    def succeeded(self, result: EngineResult) -> None: ...
    def failed(self, reason: str) -> None: ...


class RunObserver(Protocol):
    """Records each engine run (traces, ADR-0016). Must never fail the task."""

    def observe(self, job: EngineJob) -> AbstractAsyncContextManager[RunObservation]: ...
