from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, cast

import pytest
from claude_agent_sdk import AssistantMessage, ResultMessage, SystemMessage, TextBlock
from pydantic import SecretStr

from enikk_contracts.agents import PlatformTool, ToolPolicy
from enikk_contracts.prompts import (
    PromptCandidateCreated,
    PromptCandidateRequest,
    PromptConfigChange,
    PromptRead,
    PromptReadRequest,
)
from enikk_contracts.tasks import DelegateRequest, DelegationAccepted
from enikk_core_client.errors import CoreConflictError, CoreUnavailableError
from enikk_engine_claude.engine import ClaudeEngine, ClaudeEngineConfig
from enikk_engine_claude.platform_tools import (
    DELEGATE_TOOL,
    PROPOSE_PROMPT_TOOL,
    READ_PROMPT_TOOL,
    delegate_tool,
    granted_platform_tools,
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

pytestmark = pytest.mark.anyio


class _Delegator:
    def __init__(self, error: Exception | None = None) -> None:
        self.calls: list[tuple[uuid.UUID, DelegateRequest]] = []
        self._error = error

    async def delegate_task(
        self, task_id: uuid.UUID, request: DelegateRequest
    ) -> DelegationAccepted:
        self.calls.append((task_id, request))
        if self._error is not None:
            raise self._error
        return DelegationAccepted(task_id=uuid.uuid4(), agent=request.agent, duplicate=False)


def _job(delegates: tuple[str, ...] = ("developer",)) -> EngineJob:
    base = definition()
    return EngineJob(
        task_id=uuid.uuid4(),
        lease_id=uuid.uuid4(),
        attempt=1,
        session_id=uuid.uuid4(),
        engine_session_id=None,
        definition=base.model_copy(update={"delegates": delegates}),
        prompt=ResolvedPrompt(name="assistant", version=1, text="hi", model="claude-test-1"),
        input="please change the README",
    )


async def test_delegate_sends_only_the_request_under_the_lease() -> None:
    job = _job()
    delegator = _Delegator()
    result = await delegate_tool(job, delegator).handler(
        {"agent": "developer", "request": "Add a line to the README"}
    )
    assert not result["is_error"]
    assert "Accepted by developer" in result["content"][0]["text"]
    [(task_id, request)] = delegator.calls
    assert task_id == job.task_id
    assert request == DelegateRequest(
        lease_id=job.lease_id, agent="developer", request="Add a line to the README"
    )


@pytest.mark.parametrize(
    ("error", "message"),
    [
        (CoreConflictError("daily limit reached", status=409), "refused: daily limit"),
        (CoreUnavailableError("down"), "unreachable"),
    ],
)
async def test_refusals_are_reported_to_the_model(error: Exception, message: str) -> None:
    result = await delegate_tool(_job(), _Delegator(error)).handler(
        {"agent": "developer", "request": "x"}
    )
    assert result["is_error"]
    assert message in result["content"][0]["text"]


async def test_empty_request_is_rejected_before_core() -> None:
    delegator = _Delegator()
    result = await delegate_tool(_job(), delegator).handler({"agent": "developer", "request": ""})
    assert result["is_error"]
    assert delegator.calls == []


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


def _engine(script: _Script, tmp_path: Path, delegator: _Delegator | None) -> ClaudeEngine:
    return ClaudeEngine(
        config=ClaudeEngineConfig(workdir=tmp_path / "w", scratch_dir=tmp_path / "s"),
        oauth_token=SecretStr("sk-ant-oat-test"),
        transcripts=cast("TranscriptApi", _NoTranscripts()),
        delegator=delegator,
        query_fn=script,
    )


class _NoTranscripts:
    async def append_transcript(self, *args: object) -> object:
        raise AssertionError("not used")

    async def load_transcript(self, *args: object) -> object:
        raise AssertionError("not used")

    async def transcript_subpaths(self, *args: object) -> object:
        raise AssertionError("not used")


async def test_agents_with_delegates_get_only_the_delegate_tool(tmp_path: Path) -> None:
    script = _Script([DELEGATE_TOOL])
    await _engine(script, tmp_path, _Delegator()).run(_job())
    assert script.options is not None
    assert script.options.tools == []
    assert script.options.allowed_tools == [DELEGATE_TOOL]
    assert isinstance(script.options.mcp_servers, dict)
    assert list(script.options.mcp_servers) == ["enikk"]


async def test_agents_without_delegates_get_no_platform_tools(tmp_path: Path) -> None:
    script = _Script([])
    await _engine(script, tmp_path, _Delegator()).run(_job(delegates=()))
    assert script.options is not None
    assert script.options.allowed_tools == []
    assert script.options.mcp_servers == {}


async def test_the_delegate_tool_is_refused_without_a_delegator(tmp_path: Path) -> None:
    script = _Script([DELEGATE_TOOL])
    with pytest.raises(Exception, match="delegate"):
        await _engine(script, tmp_path, None).run(_job())


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
    result = await propose_prompt_tool(job, proposer).handler(
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
    result = await propose_prompt_tool(_granted_job(), proposer).handler(
        {"prompt": "assistant", "text": "", "reason": "x"}
    )
    assert result["is_error"]
    assert proposer.calls == []


async def test_propose_prompt_reports_core_refusals() -> None:
    error = CoreConflictError('the config of the candidate has no "model"', status=422)
    result = await propose_prompt_tool(_granted_job(), _Proposer(error)).handler(
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
    result = await read_prompt_tool(job, proposer).handler({"prompt": "developer", "version": 4})
    assert not result["is_error"]
    assert "You are enikk." in result["content"][0]["text"]
    [request] = proposer.reads
    assert request == PromptReadRequest(lease_id=job.lease_id, prompt="developer", version=4)
    both = await read_prompt_tool(job, proposer).handler(
        {"prompt": "developer", "label": "candidate", "version": 4}
    )
    assert both["is_error"]
    assert len(proposer.reads) == 1
