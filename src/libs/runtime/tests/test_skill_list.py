"""The skill list at the start of a session (ADR-0037 §3)."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import pytest

from enikk_contracts.agents import AgentDefinition, Execution, ToolPolicy
from enikk_contracts.skills import SkillSummary
from enikk_kernel.testing import ManualClock
from enikk_runtime.memory import HEADER as MEMORY_HEADER
from enikk_runtime.memory import SEPARATOR
from enikk_runtime.skills import HEADER, NONE, with_skills
from enikk_runtime.testing import (
    FakeCore,
    ScriptedEngine,
    StaticPromptStore,
    definition,
    prompt,
)
from enikk_runtime.worker import TaskWorker, WorkerPolicy

if TYPE_CHECKING:
    from enikk_runtime.model import LoadedSkill

pytestmark = pytest.mark.anyio

SKILLS = (SkillSummary(name="skill-developer", description="코드를 바꾸고 PR 을 만들 때"),)


class _Library:
    def __init__(self, *, broken: bool = False) -> None:
        self.listed = 0
        self._broken = broken

    async def catalog(self) -> tuple[SkillSummary, ...]:
        self.listed += 1
        if self._broken:
            raise OSError("Langfuse down")
        return SKILLS

    async def load(self, name: str) -> LoadedSkill:
        raise AssertionError("the worker never loads a skill")


async def _tick(_: float) -> None:
    await asyncio.sleep(0.001)


def _with_tools(**changes: object) -> AgentDefinition:
    return definition().model_copy(
        update={"tools": ToolPolicy(builtin=("Read",), **changes)}  # type: ignore[arg-type]
    )


async def _run(agent: AgentDefinition, library: _Library) -> ScriptedEngine:
    core = FakeCore(ManualClock())
    core.assign(agent, text="리뷰해 줘")
    engine = ScriptedEngine()
    worker = TaskWorker(
        core=core,
        prompts=StaticPromptStore(prompt()),
        engine=engine,
        clock=ManualClock(),
        policy=WorkerPolicy(),
        sleep=_tick,
        skills=library,
    )
    await worker.run_once()
    return engine


async def test_a_shared_role_with_tools_gets_the_skill_list() -> None:
    library = _Library()
    engine = await _run(_with_tools(), library)
    [job] = engine.jobs
    assert job.skills == SKILLS
    assert job.input.startswith(HEADER)
    assert "- skill-developer: 코드를 바꾸고 PR 을 만들 때" in job.input
    assert job.input.endswith(f"{SEPARATOR}\n리뷰해 줘")


@pytest.mark.parametrize(
    "agent",
    [
        definition(),
        _with_tools().model_copy(update={"execution": Execution.ISOLATED}),
        _with_tools(platform_defaults=False),
    ],
    ids=["no-tools", "isolated", "no-defaults"],
)
async def test_other_roles_get_no_skills(agent: AgentDefinition) -> None:
    library = _Library()
    engine = await _run(agent, library)
    [job] = engine.jobs
    assert job.skills is None
    assert library.listed == 0
    assert job.input == "리뷰해 줘"


async def test_an_unreachable_store_gives_an_empty_list_but_keeps_load_skill() -> None:
    engine = await _run(_with_tools(), _Library(broken=True))
    [job] = engine.jobs
    assert job.skills == ()
    assert NONE in job.input


def test_the_list_goes_in_front_of_other_headers() -> None:
    text = with_skills(f"{MEMORY_HEADER}\n- a\n\n{SEPARATOR}\nhi", SKILLS)
    assert text.startswith(HEADER)
    assert text.count(SEPARATOR) == 1
    assert with_skills("hi", None) == "hi"
