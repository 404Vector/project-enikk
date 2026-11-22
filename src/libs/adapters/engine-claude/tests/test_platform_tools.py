from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, cast

import pytest
from claude_agent_sdk import AssistantMessage, ResultMessage, SystemMessage, TextBlock
from pydantic import SecretStr

from enikk_contracts.tasks import DelegateRequest, DelegationAccepted
from enikk_core_client.errors import CoreConflictError, CoreUnavailableError
from enikk_engine_claude.engine import ClaudeEngine, ClaudeEngineConfig
from enikk_engine_claude.platform_tools import DELEGATE_TOOL, delegate_tool
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
