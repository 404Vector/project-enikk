"""A session's pull request: bundle the worktree's commits and hand them to core
(ADR-0037 §6b). The model never gets a GitHub token or runs ``git push``."""

from __future__ import annotations

import asyncio
import base64
from typing import TYPE_CHECKING

from enikk_contracts.pulls import PullSubmission

if TYPE_CHECKING:
    from enikk_contracts.pulls import PullSubmitted
    from enikk_runtime.model import EngineJob
    from enikk_runtime.ports import PullSubmitter, Workspaces


class SessionPulls:
    def __init__(self, workspaces: Workspaces, core: PullSubmitter) -> None:
        self._workspaces = workspaces
        self._core = core

    async def submit(self, job: EngineJob, title: str | None, body: str | None) -> PullSubmitted:
        """Raises ``WorkspaceError`` when there is nothing to send, and the core client's
        errors when core refuses or cannot be reached."""
        bundle = await asyncio.to_thread(self._workspaces.bundle, job.session_id)
        request = PullSubmission(
            lease_id=job.lease_id,
            bundle=base64.b64encode(bundle.data).decode("ascii"),
            base=bundle.base,
            head=bundle.head,
            title=title,
            body=body,
        )
        return await self._core.submit_pull(job.task_id, request)
