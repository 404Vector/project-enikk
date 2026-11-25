"""A thread's Claude process kept between its tasks (ADR-0041 §3, 4b)."""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, ClassVar

import pytest
from claude_agent_sdk import AssistantMessage, ResultMessage, SystemMessage, TextBlock
from pydantic import SecretStr

from enikk_engine_claude.engine import ClaudeEngine, ClaudeEngineConfig
from enikk_engine_claude.live import LivePolicy, LivePool, LiveSession
from enikk_runtime.errors import EngineError
from enikk_runtime.model import EngineJob, ResolvedPrompt
from enikk_runtime.testing import definition

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from claude_agent_sdk import ClaudeAgentOptions, Message

    from enikk_engine_claude.current import CurrentJob

pytestmark = pytest.mark.anyio


class _Transcripts:
    async def append_transcript(self, task_id: uuid.UUID, request: object) -> object:
        raise AssertionError("the fake client saves nothing")

    async def load_transcript(self, task_id: uuid.UUID, query: object) -> object:
        raise AssertionError("no resume in these tests")

    async def list_transcript_subpaths(self, task_id: uuid.UUID, query: object) -> object:
        raise AssertionError


class _Client:
    """Answers every query with one turn; records what the engine asked."""

    made: ClassVar[list[_Client]] = []

    def __init__(self, options: ClaudeAgentOptions, *, fail: bool = False) -> None:
        self.options = options
        self.fail = fail
        self.connected = 0
        self.queries: list[str] = []
        self.disconnected = False
        _Client.made.append(self)

    async def connect(self) -> None:
        self.connected += 1

    async def query(self, prompt: str) -> None:
        self.queries.append(prompt)

    async def receive_response(self) -> AsyncIterator[Message]:
        if len(self.queries) == 1:
            yield SystemMessage(subtype="init", data={"tools": []})
        if self.fail:
            yield _result(is_error=True, subtype="error_during_execution")
            return
        yield AssistantMessage(content=[TextBlock(text="done")], model="claude-test-1")
        yield _result()

    async def disconnect(self) -> None:
        self.disconnected = True


def _result(**overrides: Any) -> ResultMessage:  # noqa: ANN401
    fields: dict[str, Any] = {
        "subtype": "success",
        "duration_ms": 1,
        "duration_api_ms": 1,
        "is_error": False,
        "num_turns": 1,
        "session_id": "claude-session-1",
        "result": "done",
    }
    fields.update(overrides)
    return ResultMessage(**fields)


def _job(session: uuid.UUID, *, version: int = 1, text: str = "hello") -> EngineJob:
    return EngineJob(
        task_id=uuid.uuid4(),
        lease_id=uuid.uuid4(),
        attempt=1,
        session_id=session,
        engine_session_id=None,
        definition=definition(),
        prompt=ResolvedPrompt(
            name="enikk", version=version, text="You are enikk.", model="claude-test-1"
        ),
        input=text,
    )


ONE_OFF: list[str] = []


async def _one_off(*, prompt: str, options: ClaudeAgentOptions) -> AsyncIterator[Message]:
    """The one-off run the engine falls back to; it fails too."""
    ONE_OFF.append(prompt)
    yield _result(is_error=True, subtype="error_during_execution")


def _engine(tmp_path: Path, *, fail: bool = False) -> ClaudeEngine:
    _Client.made = []
    return ClaudeEngine(
        config=ClaudeEngineConfig(
            workdir=tmp_path / "work", scratch_dir=tmp_path / "scratch", live=LivePolicy()
        ),
        oauth_token=SecretStr("sk-ant-oat-test"),
        transcripts=_Transcripts(),  # type: ignore[arg-type]
        client_fn=lambda options: _Client(options, fail=fail),  # type: ignore[arg-type,return-value]
        query_fn=_one_off,
        landlock=False,
    )


async def test_the_next_task_of_a_thread_reuses_its_process(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    session = uuid.uuid4()
    first, second = _job(session, text="one"), _job(session, text="two")
    assert (await engine.run(first)).text == "done"
    assert (await engine.run(second)).text == "done"
    [client] = _Client.made
    assert client.connected == 1
    assert client.queries == ["one", "two"]
    await engine.aclose()
    assert client.disconnected


async def test_threads_and_prompt_versions_get_their_own_process(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    session = uuid.uuid4()
    await engine.run(_job(session))
    await engine.run(_job(uuid.uuid4()))
    await engine.run(_job(session, version=2))
    await asyncio.sleep(0)
    assert len(_Client.made) == 3
    assert _Client.made[0].disconnected
    await engine.aclose()


async def test_a_failed_run_closes_the_process_and_falls_back(tmp_path: Path) -> None:
    """A new live process that never answered hands the task to a one-off run."""
    engine = _engine(tmp_path, fail=True)
    ONE_OFF.clear()
    with pytest.raises(EngineError):
        await engine.run(_job(uuid.uuid4(), text="try"))
    await asyncio.sleep(0)
    [client] = _Client.made
    assert client.disconnected
    assert ONE_OFF == ["try"]


async def test_idle_sessions_are_closed(tmp_path: Path) -> None:
    pool = LivePool(LivePolicy(idle_seconds=60, max_sessions=2))
    now = _now()

    def live(at: timedelta) -> LiveSession:
        client = _Client(None)  # type: ignore[arg-type]
        home, cwd = tmp_path / str(uuid.uuid4()), tmp_path / str(uuid.uuid4())
        current: CurrentJob = None  # type: ignore[assignment]
        made = LiveSession(
            session_id=uuid.uuid4(),
            client=client,  # type: ignore[arg-type]
            current=current,
            home=home,
            cwd=cwd,
            prompt=("enikk", 1, "m"),
            loaded=[],
            now=now + at,
        )
        pool.add(made)
        pool.release(made, now + at)
        return made

    old, fresh = live(timedelta()), live(timedelta(seconds=50))
    pool.sweep(now + timedelta(seconds=70))
    await asyncio.sleep(0)
    assert old.client.disconnected  # type: ignore[attr-defined]
    assert not fresh.client.disconnected  # type: ignore[attr-defined]


def _now() -> datetime:
    return datetime.now(UTC)
