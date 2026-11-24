"""Telling a session which skills it can load (ADR-0037 §3)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from enikk_contracts.agents import Execution
from enikk_runtime.memory import HEADER as MEMORY_HEADER
from enikk_runtime.memory import SEPARATOR
from enikk_runtime.workspace import HEADER as WORKSPACE_HEADER

if TYPE_CHECKING:
    from collections.abc import Sequence

    from enikk_contracts.agents import AgentDefinition
    from enikk_contracts.skills import SkillSummary

HEADER: Final = (
    "[스킬 — 일에 맞는 스킬이 있으면 load_skill(name) 으로 본문을 불러 그 절차를 따른다. "
    "필요할 때만 부른다]"
)
NONE: Final = "- 지금 쓸 수 있는 스킬이 없다."


def uses_skills(definition: AgentDefinition) -> bool:
    """Whether a role's sessions get the skill list and ``load_skill``: shared roles with
    built-in tools and the default platform tools (today enikk)."""
    return (
        definition.execution is Execution.SHARED
        and bool(definition.tools.builtin)
        and definition.tools.platform_defaults
    )


def with_skills(text: str, skills: Sequence[SkillSummary] | None) -> str:
    """Put the skill list in front of a task's input. ``None``: the session has no skills."""
    if skills is None:
        return text
    lines = [f"- {s.name}: {s.description}" for s in skills] or [NONE]
    block = "\n".join([HEADER, *lines])
    if text.startswith((MEMORY_HEADER, WORKSPACE_HEADER)):
        return f"{block}\n\n{text}"
    return f"{block}\n\n{SEPARATOR}\n{text}"
