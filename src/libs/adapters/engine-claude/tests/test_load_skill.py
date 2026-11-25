"""``load_skill`` and the skill config of ``propose_prompt`` (ADR-0037 §2, §3)."""

from __future__ import annotations

import uuid

import pytest

from enikk_contracts.prompts import PromptCandidateCreated, PromptCandidateRequest
from enikk_contracts.skills import Capability, SkillSummary
from enikk_engine_claude.current import CurrentJob
from enikk_engine_claude.platform_tools import (
    LOAD_SKILL_TOOL,
    granted_platform_tools,
    load_skill_tool,
    propose_prompt_tool,
)
from enikk_runtime.errors import PromptInvalidError, PromptUnavailableError
from enikk_runtime.model import EngineJob, LoadedSkill, ResolvedPrompt, SkillUse
from enikk_runtime.testing import definition

pytestmark = pytest.mark.anyio

SKILL = LoadedSkill(
    name="skill-developer",
    version=4,
    text="docs/ 를 먼저 읽는다.",
    description="코드를 바꿀 때",
    requires=(Capability.PR_WRITE,),
)


class _Library:
    def __init__(self, error: Exception | None = None) -> None:
        self._error = error

    async def catalog(self) -> tuple[SkillSummary, ...]:
        return (SkillSummary(name=SKILL.name, description=SKILL.description),)

    async def load(self, name: str) -> LoadedSkill:
        if self._error is not None:
            raise self._error
        if name != SKILL.name:
            raise PromptInvalidError(f"skill '{name}' has no production version in Langfuse")
        return SKILL


def _job(skills: tuple[SkillSummary, ...] | None) -> EngineJob:
    return EngineJob(
        task_id=uuid.uuid4(),
        lease_id=uuid.uuid4(),
        attempt=1,
        session_id=uuid.uuid4(),
        engine_session_id=None,
        definition=definition(),
        prompt=ResolvedPrompt(name="assistant", version=1, text="hi", model="claude-test-1"),
        input="hi",
        skills=skills,
    )


LISTED = (SkillSummary(name=SKILL.name, description=SKILL.description),)


def test_load_skill_goes_only_to_sessions_with_a_skill_list() -> None:
    assert LOAD_SKILL_TOOL in granted_platform_tools(_job(LISTED), skills=True)
    assert LOAD_SKILL_TOOL in granted_platform_tools(_job(()), skills=True)
    assert LOAD_SKILL_TOOL not in granted_platform_tools(_job(None), skills=True)
    assert LOAD_SKILL_TOOL not in granted_platform_tools(_job(LISTED), skills=False)


async def test_load_skill_returns_the_text_and_records_the_version() -> None:
    loaded: list[SkillUse] = []
    result = await load_skill_tool(CurrentJob(_job(LISTED)), _Library(), loaded).handler(
        {"name": "skill-developer"}
    )
    assert not result["is_error"]
    text = result["content"][0]["text"]
    assert text.startswith("[skill skill-developer v4; requires: pr.write]")
    assert SKILL.text in text
    assert loaded == [SkillUse(name="skill-developer", version=4)]


@pytest.mark.parametrize(
    ("library", "name", "message"),
    [
        (_Library(), "skill-other", "No such skill"),
        (_Library(PromptUnavailableError("down")), "skill-developer", "unreachable"),
    ],
)
async def test_load_skill_reports_failures(library: _Library, name: str, message: str) -> None:
    loaded: list[SkillUse] = []
    result = await load_skill_tool(CurrentJob(_job(LISTED)), library, loaded).handler(
        {"name": name}
    )
    assert result["is_error"]
    assert message in result["content"][0]["text"]
    assert loaded == []


class _Proposer:
    def __init__(self) -> None:
        self.calls: list[PromptCandidateRequest] = []

    async def propose_prompt(
        self, task_id: uuid.UUID, request: PromptCandidateRequest
    ) -> PromptCandidateCreated:
        self.calls.append(request)
        return PromptCandidateCreated(
            promotion_id=uuid.uuid4(),
            prompt=request.prompt,
            candidate_version=1,
            production_version=None,
        )

    async def read_prompt(self, task_id: uuid.UUID, request: object) -> object:
        raise AssertionError


async def test_propose_prompt_passes_description_and_requires() -> None:
    proposer = _Proposer()
    result = await propose_prompt_tool(CurrentJob(_job(None)), proposer).handler(  # type: ignore[arg-type]
        {
            "prompt": "skill-developer",
            "text": "docs/ 를 먼저 읽는다.",
            "reason": "새 스킬",
            "config": {
                "model": "claude-a",
                "description": "코드를 바꿀 때",
                "requires": ["repo.push", "pr.write"],
            },
        }
    )
    assert not result["is_error"]
    [request] = proposer.calls
    assert request.config is not None
    assert request.config.description == "코드를 바꿀 때"
    assert request.config.requires == (Capability.REPO_PUSH, Capability.PR_WRITE)


@pytest.mark.parametrize("requires", [["cluster.write"], "repo.push"])
async def test_propose_prompt_refuses_unknown_requires(requires: object) -> None:
    proposer = _Proposer()
    result = await propose_prompt_tool(CurrentJob(_job(None)), proposer).handler(  # type: ignore[arg-type]
        {
            "prompt": "skill-developer",
            "text": "x",
            "reason": "y",
            "config": {"model": "m", "description": "d", "requires": requires},
        }
    )
    assert result["is_error"]
    assert "repo.push, pr.write, prompt.propose" in result["content"][0]["text"]
    assert proposer.calls == []
