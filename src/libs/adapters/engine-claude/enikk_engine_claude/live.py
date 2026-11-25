"""Claude sessions kept alive between the tasks of a thread (ADR-0041 §3, stage 4b).

A thread's next task goes to the same Claude Code process instead of starting a new one
and resuming from core's transcript. A live session is closed when it has been idle
for ``idle``, when it is older than ``max_age`` (its MCP token, issued for the first
task, must not expire under it), when its prompt changes, when a run on it fails, or to
make room. Core's transcript stays the source: a closed session is resumed from it.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import shutil
from datetime import timedelta
from typing import TYPE_CHECKING

from enikk_kernel.models import FrozenModel

if TYPE_CHECKING:
    import uuid
    from datetime import datetime
    from pathlib import Path

    from claude_agent_sdk import ClaudeSDKClient

    from enikk_engine_claude.current import CurrentJob
    from enikk_runtime.model import SkillUse

_log = logging.getLogger(__name__)


class LivePolicy(FrozenModel):
    idle_seconds: int = 600
    max_age_seconds: int = 3600
    """Within the MCP token's lifetime: core adds this much to it (``LIVE_SECONDS``)."""
    max_sessions: int = 3


class LiveSession:
    def __init__(
        self,
        *,
        session_id: uuid.UUID,
        client: ClaudeSDKClient,
        current: CurrentJob,
        home: Path,
        cwd: Path,
        prompt: tuple[str, int, str],
        loaded: list[SkillUse],
        now: datetime,
    ) -> None:
        self.session_id = session_id
        self.client = client
        self.current = current
        self.home = home
        self.cwd = cwd
        self.prompt = prompt
        """Name, version and model the process started with."""
        self.loaded = loaded
        self.started_at = now
        self.last_used = now
        self.busy = False


class LivePool:
    def __init__(self, policy: LivePolicy) -> None:
        self._policy = policy
        self._sessions: dict[uuid.UUID, LiveSession] = {}

    def take(
        self, session_id: uuid.UUID, prompt: tuple[str, int, str], now: datetime
    ) -> LiveSession | None:
        """The live session to reuse, or ``None``. One that cannot serve is dropped."""
        live = self._sessions.get(session_id)
        if live is None:
            return None
        if live.prompt != prompt or now - live.started_at >= _seconds(self._policy.max_age_seconds):
            self._sessions.pop(session_id, None)
            self._closing(live, "prompt changed" if live.prompt != prompt else "too old")
            return None
        live.busy = True
        return live

    def add(self, live: LiveSession) -> None:
        live.busy = True
        self._sessions[live.session_id] = live

    def release(self, live: LiveSession, now: datetime) -> None:
        live.busy = False
        live.last_used = now

    def drop(self, live: LiveSession, why: str) -> None:
        if self._sessions.get(live.session_id) is live:
            self._sessions.pop(live.session_id)
        self._closing(live, why)

    def sweep(self, now: datetime) -> None:
        """Close idle sessions, and the least recently used while there are too many."""
        idle = _seconds(self._policy.idle_seconds)
        for live in list(self._sessions.values()):
            if not live.busy and now - live.last_used >= idle:
                self.drop(live, "idle")
        free = sorted((s for s in self._sessions.values() if not s.busy), key=lambda s: s.last_used)
        while len(self._sessions) >= self._policy.max_sessions and free:
            self.drop(free.pop(0), "making room")

    async def close_all(self) -> None:
        for live in list(self._sessions.values()):
            self._sessions.pop(live.session_id)
            await close(live, "stopping")

    def _closing(self, live: LiveSession, why: str) -> None:
        task = asyncio.get_running_loop().create_task(close(live, why))
        _pending.add(task)
        task.add_done_callback(_pending.discard)


_pending: set[asyncio.Task[None]] = set()


async def close(live: LiveSession, why: str) -> None:
    _log.info("session %s: closing its Claude process (%s)", live.session_id, why)
    with contextlib.suppress(Exception):
        await live.client.disconnect()
    shutil.rmtree(live.cwd, ignore_errors=True)
    shutil.rmtree(live.home, ignore_errors=True)


def _seconds(value: int) -> timedelta:
    return timedelta(seconds=value)
