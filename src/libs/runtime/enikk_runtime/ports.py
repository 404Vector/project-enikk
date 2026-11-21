"""Ports of the runtime framework. Engines and prompt stores live in adapter packages."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    import uuid

    from enikk_contracts.agents import PromptRef
    from enikk_contracts.common import Engine
    from enikk_contracts.tasks import (
        Heartbeat,
        LeaseExtended,
        LeaseRequest,
        LeaseResponse,
        ResultAccepted,
        ResultReport,
    )
    from enikk_runtime.model import EngineJob, EngineResult, ResolvedPrompt


class TaskSource(Protocol):
    """The task half of the core API. ``CoreClient`` implements it."""

    async def lease_task(self, request: LeaseRequest) -> LeaseResponse: ...
    async def heartbeat(self, task_id: uuid.UUID, beat: Heartbeat) -> LeaseExtended: ...
    async def report_result(self, task_id: uuid.UUID, report: ResultReport) -> ResultAccepted: ...


class PromptStore(Protocol):
    async def get(self, ref: PromptRef) -> ResolvedPrompt:
        """Fetch a prompt. Raises ``PromptUnavailableError`` instead of falling back (I-13)."""
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
