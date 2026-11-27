from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any, cast

import pytest
from claude_agent_sdk import AssistantMessage, ResultMessage, SystemMessage, TextBlock
from pydantic import SecretStr

from enikk_contracts.agents import PlatformTool, ToolPolicy
from enikk_contracts.memory import NoteContent, NoteRead, NoteWrite, NoteWritten
from enikk_contracts.prompts import (
    PromptCandidateCreated,
    PromptCandidateRequest,
    PromptConfigChange,
    PromptRead,
    PromptReadRequest,
)
from enikk_core_client.errors import CoreConflictError
from enikk_engine_claude.current import CurrentJob
from enikk_engine_claude.engine import ClaudeEngine, ClaudeEngineConfig
from enikk_engine_claude.platform_tools import (
    PROPOSE_PROMPT_TOOL,
    READ_PROMPT_TOOL,
    granted_platform_tools,
    memory_tools,
    platform_server,
    propose_prompt_tool,
    read_prompt_tool,
)
from enikk_runtime.model import EngineJob, ResolvedPrompt
from enikk_runtime.testing import definition

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from claude_agent_sdk import ClaudeAgentOptions, Message

    from enikk_engine_claude.transcripts import TranscriptApi
    from enikk_runtime.ports import MemoryKeeper

pytestmark = pytest.mark.anyio


def _job() -> EngineJob:
    base = definition()
    return EngineJob(
        task_id=uuid.uuid4(),
        lease_id=uuid.uuid4(),
        attempt=1,
        session_id=uuid.uuid4(),
        engine_session_id=None,
        definition=base,
        prompt=ResolvedPrompt(name="assistant", version=1, text="hi", model="claude-test-1"),
        input="please change the README",
    )


class _Script:
    def __init__(self, tools: list[str]) -> None:
        self.options: ClaudeAgentOptions | None = None
        self._tools = tools

    async def __call__(self, *, prompt: str, options: ClaudeAgentOptions) -> AsyncIterator[Message]:
        self.options = options
        yield SystemMessage(subtype="init", data={"tools": self._tools})
        yield AssistantMessage(content=[TextBlock(text="ok")], model="claude-test-1")
        yield ResultMessage(
            subtype="success",
            duration_ms=1,
            duration_api_ms=1,
            is_error=False,
            num_turns=1,
            session_id="s",
            result="ok",
        )


def _engine(script: _Script, tmp_path: Path) -> ClaudeEngine:
    return ClaudeEngine(
        config=ClaudeEngineConfig(workdir=tmp_path / "w", scratch_dir=tmp_path / "s"),
        oauth_token=SecretStr("sk-ant-oat-test"),
        transcripts=cast("TranscriptApi", _NoTranscripts()),
        query_fn=script,
    )


class _NoTranscripts:
    async def append_transcript(self, *args: object) -> object:
        raise AssertionError("not used")

    async def load_transcript(self, *args: object) -> object:
        raise AssertionError("not used")

    async def transcript_subpaths(self, *args: object) -> object:
        raise AssertionError("not used")


async def test_an_engine_without_core_services_gives_no_platform_tools(tmp_path: Path) -> None:
    script = _Script([])
    await _engine(script, tmp_path).run(_job())
    assert script.options is not None
    assert script.options.allowed_tools == []
    assert script.options.mcp_servers == {}


# ---------------------------------------------------------------- propose_prompt (ADR-0032)


class _Proposer:
    def __init__(self, error: Exception | None = None) -> None:
        self.calls: list[tuple[uuid.UUID, PromptCandidateRequest]] = []
        self.reads: list[PromptReadRequest] = []
        self._error = error

    async def propose_prompt(
        self, task_id: uuid.UUID, request: PromptCandidateRequest
    ) -> PromptCandidateCreated:
        self.calls.append((task_id, request))
        if self._error is not None:
            raise self._error
        return PromptCandidateCreated(
            promotion_id=uuid.uuid4(),
            prompt=request.prompt,
            candidate_version=5,
            production_version=3,
        )

    async def read_prompt(self, task_id: uuid.UUID, request: PromptReadRequest) -> PromptRead:
        self.reads.append(request)
        if self._error is not None:
            raise self._error
        return PromptRead(
            prompt=request.prompt,
            version=6,
            labels=("production",),
            type="text",
            text="You are enikk.",
            config={"model": "claude-a"},
        )


def _granted_job(*, grant: bool = True) -> EngineJob:
    job = _job()
    tools = ToolPolicy(platform=(PlatformTool.PROMPT_CANDIDATES,) if grant else ())
    return job.model_copy(update={"definition": job.definition.model_copy(update={"tools": tools})})


def test_propose_prompt_goes_only_to_granted_roles() -> None:
    assert PROPOSE_PROMPT_TOOL in granted_platform_tools(_granted_job(), prompts=True)
    assert PROPOSE_PROMPT_TOOL not in granted_platform_tools(
        _granted_job(grant=False), prompts=True
    )
    assert PROPOSE_PROMPT_TOOL not in granted_platform_tools(_granted_job(), prompts=False)


async def test_propose_prompt_sends_the_candidate_under_the_lease() -> None:
    job = _granted_job()
    proposer = _Proposer()
    result = await propose_prompt_tool(CurrentJob(job), proposer).handler(
        {
            "prompt": "assistant",
            "text": "You are enikk.",
            "reason": "shorter",
            "config": {"model": "claude-b"},
        }
    )
    assert not result["is_error"]
    assert "Candidate v5" in result["content"][0]["text"]
    assert "nothing changes until they do" in result["content"][0]["text"]
    [(task_id, request)] = proposer.calls
    assert task_id == job.task_id
    assert request == PromptCandidateRequest(
        lease_id=job.lease_id,
        prompt="assistant",
        text="You are enikk.",
        reason="shorter",
        config=PromptConfigChange(model="claude-b"),
    )


async def test_propose_prompt_refuses_bad_input_before_core() -> None:
    proposer = _Proposer()
    result = await propose_prompt_tool(CurrentJob(_granted_job()), proposer).handler(
        {"prompt": "assistant", "text": "", "reason": "x"}
    )
    assert result["is_error"]
    assert proposer.calls == []


async def test_propose_prompt_reports_core_refusals() -> None:
    error = CoreConflictError('the config of the candidate has no "model"', status=422)
    result = await propose_prompt_tool(CurrentJob(_granted_job()), _Proposer(error)).handler(
        {"prompt": "assistant", "text": "x", "reason": "y"}
    )
    assert result["is_error"]
    assert "No candidate was created" in result["content"][0]["text"]


def test_read_prompt_goes_with_propose_prompt() -> None:
    assert READ_PROMPT_TOOL in granted_platform_tools(_granted_job(), prompts=True)
    assert READ_PROMPT_TOOL not in granted_platform_tools(_granted_job(grant=False), prompts=True)


async def test_read_prompt_asks_core_and_shows_the_text() -> None:
    job = _granted_job()
    proposer = _Proposer()
    result = await read_prompt_tool(CurrentJob(job), proposer).handler(
        {"prompt": "developer", "version": 4}
    )
    assert not result["is_error"]
    assert "You are enikk." in result["content"][0]["text"]
    [request] = proposer.reads
    assert request == PromptReadRequest(lease_id=job.lease_id, prompt="developer", version=4)
    both = await read_prompt_tool(CurrentJob(job), proposer).handler(
        {"prompt": "developer", "label": "candidate", "version": 4}
    )
    assert both["is_error"]
    assert len(proposer.reads) == 1


def _with_tools(tools: ToolPolicy) -> EngineJob:
    job = _job()
    return job.model_copy(update={"definition": job.definition.model_copy(update={"tools": tools})})


def test_a_role_without_platform_defaults_gets_no_platform_tools() -> None:
    bare = _with_tools(ToolPolicy(platform_defaults=False))
    every = {"memory": True, "diagnostics": True, "prompts": True}
    assert granted_platform_tools(bare, **every) == []
    assert platform_server(CurrentJob(bare), memory=object(), diagnostics=object()) is None  # type: ignore[arg-type]
    usual = _with_tools(ToolPolicy())
    assert len(granted_platform_tools(usual, **every)) > 0


class _Notes:
    def __init__(self, error: Exception | None = None) -> None:
        self.reads: list[NoteRead] = []
        self.writes: list[NoteWrite] = []
        self._error = error

    async def read_note(self, task_id: uuid.UUID, request: NoteRead) -> NoteContent:
        self.reads.append(request)
        return NoteContent(path=request.path, exists=True, text="---\ndraft: true\n---\n본문")

    async def write_note(self, task_id: uuid.UUID, request: NoteWrite) -> NoteWritten:
        if self._error is not None:
            raise self._error
        self.writes.append(request)
        return NoteWritten(action="created", path=request.path, draft=True)


def _note_tools(notes: _Notes, job: EngineJob) -> dict[str, Any]:
    tools = memory_tools(CurrentJob(job), cast("MemoryKeeper", notes))
    return {t.name: t for t in tools}


def test_blog_tools_go_with_memory() -> None:
    granted = granted_platform_tools(_with_tools(ToolPolicy()), memory=True)
    assert {"mcp__enikk__read_note", "mcp__enikk__write_note"} <= set(granted)


async def test_blog_tools_read_and_write_under_the_lease() -> None:
    job = _job()
    notes = _Notes()
    tools = _note_tools(notes, job)
    read = await tools["read_note"].handler({"path": "vault/Garden/x.md"})
    assert not read["is_error"]
    assert "Reference only" in read["content"][0]["text"]
    assert "본문" in read["content"][0]["text"]
    written = await tools["write_note"].handler({"path": "vault/Garden/x.md", "text": "글"})
    assert not written["is_error"]
    assert "a draft, not published" in written["content"][0]["text"]
    assert notes.writes == [NoteWrite(lease_id=job.lease_id, path="vault/Garden/x.md", text="글")]


async def test_write_note_reports_refusals() -> None:
    error = CoreConflictError("a new note must have draft: true", status=403)
    tools = _note_tools(_Notes(error), _job())
    result = await tools["write_note"].handler({"path": "vault/Garden/x.md", "text": "글"})
    assert result["is_error"]
    assert "draft: true" in result["content"][0]["text"]
    empty = await tools["write_note"].handler({"path": "vault/Garden/x.md", "text": ""})
    assert empty["is_error"]
