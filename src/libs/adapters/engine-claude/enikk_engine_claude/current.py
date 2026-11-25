"""The task a Claude session is working on now (ADR-0041 §3).

Everything the CLI's session calls back into (the platform tools, the transcript store,
the audit hooks) reads the task and lease from here when it is called, not when it was
made, so one long-lived session can serve one task after another.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from enikk_runtime.model import EngineJob


class CurrentJob:
    def __init__(self, job: EngineJob) -> None:
        self.job = job
