from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any

import pytest
from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    CLIConnectionError,
    MirrorErrorMessage,
    ResultMessage,
    SystemMessage,
    TextBlock,
)
from claude_agent_sdk.testing import run_session_store_conformance
from pydantic import SecretStr

from enikk_contracts.transcripts import (
    SubpathQuery,
    TranscriptAppend,
    TranscriptAppended,
    TranscriptEntry,
    TranscriptLoaded,
    TranscriptQuery,
    TranscriptSubpaths,
)
from enikk_engine_claude.engine import ClaudeEngine, ClaudeEngineConfig
from enikk_engine_claude.transcripts import CoreSessionStore
from enikk_runtime.errors import EngineError
from enikk_runtime.model import EngineJob, ResolvedPrompt
from enikk_runtime.testing import definition

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from claude_agent_sdk import Message

pytestmark = pytest.mark.anyio


class FakeTranscripts:
    """Mimics core: per-key append order, ``uuid`` de-duplication, sorted subpaths."""

    def __init__(self) -> None:
        self.stored: dict[tuple[str, str, str], list[TranscriptEntry]] = {}

    async def append_transcript(
        self, task_id: uuid.UUID, request: TranscriptAppend
    ) -> TranscriptAppended:
        key = request.key
        slot = self.stored.setdefault(
            (key.project_key, key.engine_session_id, key.subpath or ""), []
        )
        seen = {e.get("uuid") for e in slot if isinstance(e.get("uuid"), str)}
        added = 0
        for entry in request.entries:
            marker = entry.get("uuid")
            if isinstance(marker, str):
                if marker in seen:
                    continue
                seen.add(marker)
            slot.append(entry)
            added += 1
        return TranscriptAppended(stored=added)

    async def load_transcript(self, task_id: uuid.UUID, query: TranscriptQuery) -> TranscriptLoaded:
        key = query.key
        found = self.stored.get((key.project_key, key.engine_session_id, key.subpath or ""), [])
        return TranscriptLoaded(entries=tuple(found))

    async def transcript_subpaths(
        self, task_id: uuid.UUID, query: SubpathQuery
    ) -> TranscriptSubpaths:
        subpaths = sorted(
            {
                s
                for (p, e, s) in self.stored
                if (p, e) == (query.project_key, query.engine_session_id) and s
            }
        )
        return TranscriptSubpaths(subpaths=tuple(subpaths))


async def test_session_store_passes_the_sdk_conformance_suite() -> None:
    await run_session_store_conformance(
        lambda: CoreSessionStore(FakeTranscripts(), uuid.uuid4(), uuid.uuid4()).as_sdk_store()
    )


def _job(*, resume: str | None = None, parameters: dict[str, Any] | None = None) -> EngineJob:
    return EngineJob(
        task_id=uuid.uuid4(),
        lease_id=uuid.uuid4(),
        attempt=1,
        session_id=uuid.uuid4(),
        engine_session_id=resume,
        definition=definition(),
        prompt=ResolvedPrompt(
            name="assistant",
            version=1,
            text="You are enikk.",
            model="claude-test-1",
            parameters=parameters or {},
        ),
        input="hello",
    )


def _init(tools: list[str] | None = None) -> SystemMessage:
    return SystemMessage(subtype="init", data={"tools": tools or []})


def _result(overrides: dict[str, Any] | None = None) -> ResultMessage:
    fields: dict[str, Any] = {
        "subtype": "success",
        "duration_ms": 10,
        "duration_api_ms": 8,
        "is_error": False,
        "num_turns": 1,
        "session_id": "claude-session-1",
        "result": "hi there",
        "usage": {"input_tokens": 12, "output_tokens": 4, "cache_read_input_tokens": 100},
    }
    fields.update(overrides or {})
    return ResultMessage(**fields)


class _Script:
    def __init__(self, *messages: Message, error: Exception | None = None) -> None:
        self._messages = messages
        self._error = error
        self.options: ClaudeAgentOptions | None = None
        self.prompt: str | None = None

    async def __call__(self, *, prompt: str, options: ClaudeAgentOptions) -> AsyncIterator[Message]:
        self.prompt = prompt
        self.options = options
        for message in self._messages:
            yield message
        if self._error is not None:
            raise self._error


def _engine(script: _Script, tmp_path: Path) -> ClaudeEngine:
    return ClaudeEngine(
        config=ClaudeEngineConfig(workdir=tmp_path / "work", scratch_dir=tmp_path / "scratch"),
        oauth_token=SecretStr("sk-ant-oat-test"),
        transcripts=FakeTranscripts(),
        query_fn=script,
    )


async def test_runs_without_tools_and_returns_the_answer(tmp_path: Path) -> None:
    script = _Script(
        _init(),
        AssistantMessage(content=[TextBlock(text="hi there")], model="claude-test-1-20261101"),
        _result(),
    )
    result = await _engine(script, tmp_path).run(_job(resume="claude-session-0"))

    assert result.text == "hi there"
    assert result.engine_session_id == "claude-session-1"
    assert result.model == "claude-test-1-20261101"
    assert result.usage is not None
    assert result.usage.cache_read_tokens == 100

    options = script.options
    assert options is not None
    assert options.tools == []
    assert options.setting_sources == []
    assert options.strict_mcp_config
    assert options.permission_mode == "dontAsk"
    assert options.verbatim_prompts
    assert options.system_prompt == "You are enikk."
    assert options.model == "claude-test-1"
    assert options.resume == "claude-session-0"
    assert options.max_turns == 20
    assert isinstance(options.session_store, CoreSessionStore)
    assert options.env["CLAUDE_CODE_OAUTH_TOKEN"] == "sk-ant-oat-test"
    assert options.env["ANTHROPIC_API_KEY"] == ""
    assert options.env["CLAUDE_CONFIG_DIR"].startswith(str(tmp_path / "scratch"))
    assert script.prompt == "hello"


async def test_effort_comes_from_the_prompt_config(tmp_path: Path) -> None:
    script = _Script(_init(), _result())
    await _engine(script, tmp_path).run(_job(parameters={"effort": "low", "colour": "blue"}))
    assert script.options is not None
    assert script.options.effort == "low"


async def test_exposed_built_in_tools_abort_the_run(tmp_path: Path) -> None:
    script = _Script(_init(["Bash", "Read"]), _result())
    with pytest.raises(EngineError, match="Bash") as raised:
        await _engine(script, tmp_path).run(_job())
    assert not raised.value.retryable


async def test_transcript_mirror_failure_is_retryable(tmp_path: Path) -> None:
    mirror_error = MirrorErrorMessage(subtype="mirror_error", data={}, error="core down")
    script = _Script(_init(), mirror_error, _result())
    with pytest.raises(EngineError, match="saved to core") as raised:
        await _engine(script, tmp_path).run(_job())
    assert raised.value.retryable


@pytest.mark.parametrize(
    ("overrides", "retryable"),
    [
        ({"subtype": "error_max_turns", "is_error": True}, False),
        ({"subtype": "error_during_execution", "is_error": True}, True),
        ({"is_error": True, "api_error_status": 429}, True),
        ({"is_error": True, "api_error_status": 401}, False),
    ],
)
async def test_error_results_are_classified(
    tmp_path: Path, overrides: dict[str, Any], retryable: bool
) -> None:
    script = _Script(_init(), _result(overrides))
    with pytest.raises(EngineError) as raised:
        await _engine(script, tmp_path).run(_job())
    assert raised.value.retryable is retryable


async def test_cli_failure_is_retryable_and_hides_details(tmp_path: Path) -> None:
    script = _Script(_init(), error=CLIConnectionError("token sk-ant-oat-test rejected"))
    with pytest.raises(EngineError) as raised:
        await _engine(script, tmp_path).run(_job())
    assert raised.value.retryable
    assert "sk-ant" not in raised.value.reason


async def test_missing_result_and_empty_answer_fail(tmp_path: Path) -> None:
    with pytest.raises(EngineError, match="without a result"):
        await _engine(_Script(_init()), tmp_path).run(_job())
    with pytest.raises(EngineError, match="empty"):
        await _engine(_Script(_init(), _result({"result": "  "})), tmp_path).run(_job())


async def test_scratch_home_is_removed_after_the_run(tmp_path: Path) -> None:
    await _engine(_Script(_init(), _result()), tmp_path).run(_job())
    assert list((tmp_path / "scratch").iterdir()) == []


def test_only_claude_models_are_accepted(tmp_path: Path) -> None:
    engine = _engine(_Script(), tmp_path)
    assert engine.accepts_model("claude-opus-5-5")
    assert not engine.accepts_model("gemini-3-pro")
