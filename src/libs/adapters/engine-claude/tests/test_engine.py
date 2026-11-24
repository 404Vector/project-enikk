from __future__ import annotations

import os
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    CLIConnectionError,
    MirrorErrorMessage,
    ResultError,
    ResultMessage,
    SystemMessage,
    TextBlock,
    ToolUseBlock,
    project_key_for_directory,
)
from claude_agent_sdk.testing import run_session_store_conformance
from pydantic import SecretStr

from enikk_contracts.agents import Execution, McpToolGrant, ToolPolicy
from enikk_contracts.transcripts import (
    SubpathQuery,
    TranscriptAppend,
    TranscriptAppended,
    TranscriptEntry,
    TranscriptLoaded,
    TranscriptQuery,
    TranscriptSubpaths,
)
from enikk_engine_claude import sandbox
from enikk_engine_claude.engine import ClaudeEngine, ClaudeEngineConfig
from enikk_engine_claude.mcp_gateway import ENV_TOKEN
from enikk_engine_claude.platform_tools import SERVER as PLATFORM_SERVER
from enikk_engine_claude.resume import EMPTY_HEADER, with_history
from enikk_engine_claude.transcripts import CoreSessionStore
from enikk_runtime.errors import EngineError
from enikk_runtime.model import EngineJob, ResolvedPrompt, SessionWorkspace
from enikk_runtime.testing import definition

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

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
    assert options.env[sandbox.ENV_TOKEN] == "sk-ant-oat-test"
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in options.env
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


async def test_granted_tools_are_the_only_tools(tmp_path: Path) -> None:
    job = _job()
    granted = job.definition.model_copy(
        update={
            "execution": Execution.ISOLATED,
            "tools": ToolPolicy(builtin=("Bash", "Read")),
        }
    )
    job = job.model_copy(update={"definition": granted})
    script = _Script(_init(["Bash", "Read"]), _result())
    await _engine(script, tmp_path).run(job)
    assert script.options is not None
    assert script.options.tools == ["Bash", "Read"]
    assert script.options.allowed_tools == ["Bash", "Read"]

    wider = _Script(_init(["Bash", "Read", "WebFetch"]), _result())
    with pytest.raises(EngineError, match="WebFetch"):
        await _engine(wider, tmp_path).run(job)


async def test_tool_env_reaches_the_cli_but_cannot_replace_the_token(tmp_path: Path) -> None:
    script = _Script(_init(), _result())
    engine = ClaudeEngine(
        config=ClaudeEngineConfig(
            workdir=tmp_path / "work",
            scratch_dir=tmp_path / "scratch",
            tool_env={"GIT_CONFIG_GLOBAL": "/workspace/gitconfig", "CLAUDE_CODE_OAUTH_TOKEN": "x"},
        ),
        oauth_token=SecretStr("sk-ant-oat-test"),
        transcripts=FakeTranscripts(),
        query_fn=script,
    )
    await engine.run(_job())
    assert script.options is not None
    assert script.options.env["GIT_CONFIG_GLOBAL"] == "/workspace/gitconfig"
    assert script.options.env[sandbox.ENV_TOKEN] == "sk-ant-oat-test"
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in script.options.env


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
        ({"subtype": "error_during_execution", "is_error": True}, False),
        ({"is_error": True, "terminal_reason": "api_error"}, True),
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


# ---------------------------------------------------------------- failures and resume (ADR-0034)


class _Scripts:
    """One script per run, in order; records each run's prompt and options."""

    def __init__(self, *scripts: _Script) -> None:
        self._scripts = list(scripts)
        self.runs: list[_Script] = []

    def __call__(self, *, prompt: str, options: ClaudeAgentOptions) -> AsyncIterator[Message]:
        script = self._scripts.pop(0)
        self.runs.append(script)
        return script(prompt=prompt, options=options)


def _engine_with(query: Any, tmp_path: Path, transcripts: FakeTranscripts) -> ClaudeEngine:  # noqa: ANN401
    return ClaudeEngine(
        config=ClaudeEngineConfig(workdir=tmp_path / "work", scratch_dir=tmp_path / "scratch"),
        oauth_token=SecretStr("sk-ant-oat-test"),
        transcripts=transcripts,
        query_fn=query,
    )


def _refused(message: str = "No conversation found with session ID: old") -> ResultError:
    data = {"subtype": "error_during_execution", "is_error": True, "errors": [message]}
    return ResultError(f"Claude Code returned an error result: {message}", data=data)


class _StderrScript(_Script):
    """Writes ``lines`` to the CLI's stderr callback before its messages."""

    def __init__(self, lines: list[str], *messages: Message, error: Exception | None) -> None:
        super().__init__(*messages, error=error)
        self._lines = lines

    async def __call__(self, *, prompt: str, options: ClaudeAgentOptions) -> AsyncIterator[Message]:
        assert options.stderr is not None
        for line in self._lines:
            options.stderr(line)
        async for message in super().__call__(prompt=prompt, options=options):
            yield message


async def test_a_result_error_is_logged_and_reported_in_detail(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    secret = "sk-ant-oat01-" + "s" * 30
    search = ToolUseBlock(id="toolu_1", name="WebSearch", input={"query": "x"})
    error = ResultError(
        "Claude Code returned an error result: boom",
        data={
            "subtype": "error_during_execution",
            "is_error": True,
            "errors": [f"Tool failed with {secret}\nsecond line"],
            "terminal_reason": "tool_error",
        },
    )
    script = _StderrScript(
        ["starting", f"auth {secret} rejected"],
        _init(),
        AssistantMessage(content=[search], model="claude-test-1"),
        error=error,
    )
    with pytest.raises(EngineError) as raised:
        await _engine(script, tmp_path).run(_job())

    failure = raised.value
    assert not failure.retryable
    assert failure.reason == (
        "Claude stopped: error_during_execution: Tool failed with [가림] (last tool WebSearch)"
    )
    [logged] = [r.getMessage() for r in caplog.records if "Claude Code failed" in r.getMessage()]
    assert "subtype='error_during_execution'" in logged
    assert "is_error=True" in logged
    assert "terminal_reason='tool_error'" in logged
    assert "last_tool='WebSearch'" in logged
    assert "stderr=starting | auth [가림] rejected" in logged
    assert secret not in logged


async def test_a_refused_resume_starts_a_new_session_with_the_old_conversation(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    transcripts = FakeTranscripts()
    store = CoreSessionStore(transcripts, uuid.uuid4(), uuid.uuid4())
    project = project_key_for_directory(str(tmp_path / "work"))
    old_session = str(uuid.uuid4())
    entries: list[Any] = [
        {"type": "user", "uuid": "u1", "message": {"role": "user", "content": "무슨상황이야?"}},
        {
            "type": "assistant",
            "uuid": "a1",
            "message": {"content": [{"type": "text", "text": "배포가 끝났습니다."}]},
        },
    ]
    await store.append({"project_key": project, "session_id": old_session}, entries)
    fresh = _Script(_init(), _result({"session_id": "claude-session-new", "result": "ok"}))
    query = _Scripts(_Script(_init(), error=_refused()), fresh)

    result = await _engine_with(query, tmp_path, transcripts).run(_job(resume=old_session))

    assert result.text == "ok"
    assert result.engine_session_id == "claude-session-new"
    first, second = query.runs
    assert first.options is not None
    assert first.options.resume == old_session
    assert second.options is not None
    assert second.options.resume is None
    assert second.prompt is not None
    assert "이어받지 못해 새 대화로 시작한다" in second.prompt
    assert "무슨상황이야?" in second.prompt
    assert "배포가 끝났습니다." in second.prompt
    assert second.prompt.endswith("[새 메시지]\n\nhello")
    assert any("starting a new session" in r.getMessage() for r in caplog.records)


async def test_a_new_session_that_also_fails_reports_its_own_failure(tmp_path: Path) -> None:
    query = _Scripts(
        _Script(_init(), error=_refused()),
        _Script(_init(), error=_refused("the prompt is too long")),
    )
    with pytest.raises(EngineError, match="the prompt is too long") as raised:
        await _engine_with(query, tmp_path, FakeTranscripts()).run(_job(resume="old"))
    assert not raised.value.retryable
    assert len(query.runs) == 2


async def test_a_passing_api_failure_is_retried_on_the_same_session(tmp_path: Path) -> None:
    overloaded = ResultError(
        "overloaded",
        data={"subtype": "success", "is_error": True, "api_error_status": 529},
    )
    query = _Scripts(_Script(_init(), error=overloaded))
    with pytest.raises(EngineError, match="API status 529") as raised:
        await _engine_with(query, tmp_path, FakeTranscripts()).run(_job(resume="old"))
    assert raised.value.retryable
    assert len(query.runs) == 1


async def test_a_resumed_run_that_already_answered_is_not_started_again(tmp_path: Path) -> None:
    query = _Scripts(
        _Script(
            _init(),
            AssistantMessage(content=[TextBlock(text="working")], model="claude-test-1"),
            error=_refused("Bash failed"),
        )
    )
    with pytest.raises(EngineError, match="Bash failed"):
        await _engine_with(query, tmp_path, FakeTranscripts()).run(_job(resume="old"))
    assert len(query.runs) == 1


def test_history_keeps_the_newest_main_thread_text() -> None:
    entries: list[dict[str, Any]] = [
        {"type": "user", "message": {"content": "old " * 2_000}},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "a" * 1_500}]}},
        {"type": "user", "isSidechain": True, "message": {"content": "subagent"}},
        {"type": "user", "message": {"content": [{"type": "tool_result", "content": "r"}]}},
        {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Bash"}]}},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "last answer"}]}},
        {"type": "system", "message": {"content": "skip"}},
    ]
    text = with_history("new", entries)
    assert "subagent" not in text
    assert "skip" not in text
    assert text.endswith("- 답:\n" + "a" * 1_500 + "\nlast answer\n\n[새 메시지]\n\nnew")
    assert len(text) < 14_000
    assert with_history("new", []) == f"{EMPTY_HEADER}\n\nnew"


async def test_the_session_workspace_opens_only_its_own_paths(tmp_path: Path) -> None:
    data = tmp_path / "data"
    session = "00000000-0000-0000-0000-000000000001"
    workspace = SessionWorkspace(
        repository="404Vector/project-enikk",
        repo_dir=data / "repo.git",
        snapshot_dir=data / "snapshots" / ("a" * 40),
        commit="a" * 40,
        fetched_at=None,
        fresh=True,
        worktree_dir=data / "worktrees" / session,
        branch=f"session/{session}",
        tmp_dir=data / "tmp" / session,
    )
    script = _Script(_init(), _result())
    await _engine(script, tmp_path).run(_job().model_copy(update={"workspace": workspace}))

    assert script.options is not None
    read = script.options.env[sandbox.ENV_READ].split(os.pathsep)
    write = script.options.env[sandbox.ENV_WRITE].split(os.pathsep)
    assert str(data / "snapshots" / ("a" * 40)) in read
    assert str(data / "snapshots" / ("a" * 40)) not in write
    for path in (data / "repo.git", data / "worktrees" / session, data / "tmp" / session):
        assert str(path) in write
    assert str(tmp_path / "scratch") not in write
    assert str(data) not in write
    assert str(data / "worktrees") not in write


class _SeesItsDirectory(_Script):
    """Records whether the working directory existed while the CLI ran."""

    existed: bool = False

    async def __call__(self, *, prompt: str, options: ClaudeAgentOptions) -> AsyncIterator[Message]:
        self.existed = options.cwd is not None and Path(options.cwd).is_dir()
        async for message in super().__call__(prompt=prompt, options=options):
            yield message


async def test_each_shared_task_runs_in_its_own_directory_removed_afterwards(
    tmp_path: Path,
) -> None:
    first, second = _job(), _job()
    cwds: list[Path] = []
    for job in (first, second):
        script = _SeesItsDirectory(_init(), _result())
        await _engine(script, tmp_path).run(job)
        assert script.options is not None
        assert script.existed
        cwd = Path(str(script.options.cwd))
        assert cwd == tmp_path / "work" / str(job.task_id)
        write = script.options.env[sandbox.ENV_WRITE].split(os.pathsep)
        assert str(cwd) in write
        assert str(tmp_path / "work") not in write
        cwds.append(cwd)

    assert cwds[0] != cwds[1]
    assert not any(c.exists() for c in cwds)
    assert (tmp_path / "work").is_dir()


async def test_tasks_at_the_same_time_do_not_remove_each_others_files(tmp_path: Path) -> None:
    other = tmp_path / "work" / "another-task"
    other.mkdir(parents=True)
    (other / "notes.txt").write_text("still running")

    await _engine(_Script(_init(), _result()), tmp_path).run(_job())

    assert (other / "notes.txt").read_text() == "still running"


async def test_an_isolated_role_keeps_the_working_directory(tmp_path: Path) -> None:
    job = _job()
    isolated = job.definition.model_copy(update={"execution": Execution.ISOLATED})
    script = _Script(_init(), _result())
    await _engine(script, tmp_path).run(job.model_copy(update={"definition": isolated}))

    assert script.options is not None
    assert script.options.cwd == str(tmp_path / "work")


async def test_a_session_resumes_from_another_tasks_directory(tmp_path: Path) -> None:
    transcripts = FakeTranscripts()
    earlier = _job()
    project = project_key_for_directory(str(tmp_path / "work" / str(earlier.task_id)))
    saved = CoreSessionStore(
        transcripts,
        earlier.task_id,
        earlier.lease_id,
        project_key=project_key_for_directory(str(tmp_path / "work")),
    )
    session = str(uuid.uuid4())
    entries: list[Any] = [{"type": "user", "uuid": "u1", "message": {"content": "hi"}}]
    await saved.append({"project_key": project, "session_id": session}, entries)
    script = _Script(_init(), _result())

    await _engine_with(script, tmp_path, transcripts).run(_job(resume=session))

    # The SDK loads the session under the key of the new task's own directory.
    assert script.options is not None
    assert script.options.resume == session
    assert script.options.session_store is not None
    here = project_key_for_directory(str(script.options.cwd))
    assert here != project
    loaded = await script.options.session_store.load({"project_key": here, "session_id": session})
    assert loaded == entries


# ------------------------------------------------ MCP through the gateway (ADR-0039 S3)

GATEWAY = "http://enikk-agentgateway.enikk.svc"
LIST = "mcp__scheduler__list_schedules"
GET = "mcp__scheduler__get_schedule"


def _gateway_engine(script: _Script, tmp_path: Path, gateway: str | None = GATEWAY) -> ClaudeEngine:
    return ClaudeEngine(
        config=ClaudeEngineConfig(
            workdir=tmp_path / "work", scratch_dir=tmp_path / "scratch", mcp_gateway=gateway
        ),
        oauth_token=SecretStr("sk-ant-oat-test"),
        transcripts=FakeTranscripts(),
        query_fn=script,
    )


def _granted(
    *grants: McpToolGrant,
    token: str | None = "header.claims.sig",  # noqa: S107 - a fake token
) -> EngineJob:
    job = _job()
    definition = job.definition.model_copy(update={"tools": ToolPolicy(mcp=grants)})
    return job.model_copy(
        update={
            "definition": definition,
            "mcp_token": None if token is None else SecretStr(token),
        }
    )


SCHEDULER = McpToolGrant(server="scheduler", allow=("list_schedules", "get_schedule"))


async def test_granted_servers_go_through_the_gateway_with_the_token_in_the_env(
    tmp_path: Path,
) -> None:
    script = _Script(_init([LIST, GET]), _result())
    await _gateway_engine(script, tmp_path).run(_granted(SCHEDULER))

    options = script.options
    assert options is not None
    assert options.strict_mcp_config
    servers = options.mcp_servers
    assert isinstance(servers, dict)
    assert servers["scheduler"] == {
        "type": "http",
        "url": f"{GATEWAY}/mcp/scheduler",
        "headers": {"Authorization": "Bearer ${ENIKK_MCP_TOKEN}"},
    }
    assert "header.claims.sig" not in str(servers)
    assert options.env[ENV_TOKEN] == "header.claims.sig"
    assert options.allowed_tools == [LIST, GET]


def test_the_token_variable_is_hidden_from_shell_commands() -> None:
    assert sandbox.SECRET_NAME.search(ENV_TOKEN)
    assert ENV_TOKEN not in sandbox.clean_environment({ENV_TOKEN: "x"})


async def test_an_mcp_tool_the_definition_does_not_grant_aborts_the_run(tmp_path: Path) -> None:
    only_list = McpToolGrant(server="scheduler", allow=("list_schedules",))
    script = _Script(_init([LIST, GET]), _result())
    with pytest.raises(EngineError, match="get_schedule"):
        await _gateway_engine(script, tmp_path).run(_granted(only_list))


@pytest.mark.parametrize(
    ("gateway", "token", "grants"),
    [
        (None, "t", (SCHEDULER,)),
        (GATEWAY, None, (SCHEDULER,)),
        (GATEWAY, "t", ()),
        (GATEWAY, "t", (McpToolGrant(server=PLATFORM_SERVER, allow=("x",)),)),
    ],
    ids=["gateway-off", "no-token", "no-grant", "reserved-name"],
)
async def test_no_mcp_server_without_gateway_token_or_grant(
    tmp_path: Path, gateway: str | None, token: str | None, grants: tuple[McpToolGrant, ...]
) -> None:
    script = _Script(_init(), _result())
    await _gateway_engine(script, tmp_path, gateway).run(_granted(*grants, token=token))
    options = script.options
    assert options is not None
    assert options.mcp_servers == {}
    assert ENV_TOKEN not in options.env
    assert options.allowed_tools == []


async def test_tool_env_cannot_set_the_token(tmp_path: Path) -> None:
    script = _Script(_init(), _result())
    engine = ClaudeEngine(
        config=ClaudeEngineConfig(
            workdir=tmp_path / "work",
            scratch_dir=tmp_path / "scratch",
            tool_env={ENV_TOKEN: "forged"},
        ),
        oauth_token=SecretStr("sk-ant-oat-test"),
        transcripts=FakeTranscripts(),
        query_fn=script,
    )
    await engine.run(_job())
    assert script.options is not None
    assert ENV_TOKEN not in script.options.env
