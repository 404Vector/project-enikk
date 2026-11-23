"""Skills: Langfuse prompts named ``skill-*`` that a session loads when it needs them
(ADR-0037 §2, §3). Their config says when to use them and which core-enforced actions
they rely on."""

from __future__ import annotations

import re
from enum import StrEnum
from typing import TYPE_CHECKING, Annotated, Final, Self

import pydantic_core
from pydantic import Field, StringConstraints, ValidationError, model_validator

from enikk_contracts.base import Contract

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

SKILL_PREFIX: Final = "skill-"
MAX_SKILLS: Final = 50
"""At most this many skills are listed to a session."""

SKILL_NAME_PATTERN: Final = r"^skill-[a-z0-9][a-z0-9-]{0,62}$"
SkillName = Annotated[str, StringConstraints(pattern=SKILL_NAME_PATTERN)]
SkillDescription = Annotated[
    str, StringConstraints(min_length=1, max_length=200, strip_whitespace=True)
]


class Capability(StrEnum):
    """Actions core enforces for a session (ADR-0037 §6b) that a skill may rely on."""

    REPO_PUSH = "repo.push"
    PR_WRITE = "pr.write"
    PROMPT_PROPOSE = "prompt.propose"


class SkillConfig(Contract):
    """The config of a skill prompt in Langfuse. Unknown keys and values are refused."""

    model: Annotated[str, StringConstraints(min_length=1, max_length=100)]
    effort: Annotated[str, StringConstraints(min_length=1, max_length=20)] | None = None
    description: SkillDescription
    """When to use the skill, in one line."""
    requires: Annotated[tuple[Capability, ...], Field(max_length=len(Capability))] = ()

    @model_validator(mode="after")
    def _one_line_unique(self) -> Self:
        if "\n" in self.description or "\r" in self.description:
            raise ValueError("description must be one line")
        if len(set(self.requires)) != len(self.requires):
            raise ValueError("requires lists a capability twice")
        return self


def is_skill(prompt: str) -> bool:
    """Whether ``prompt`` is named as a skill (it may still be an invalid name)."""
    return prompt.startswith(SKILL_PREFIX)


def valid_skill_name(prompt: str) -> bool:
    return re.fullmatch(SKILL_NAME_PATTERN, prompt) is not None


def declared_requires(config: Mapping[str, object]) -> tuple[Capability, ...]:
    """The known capabilities in ``config["requires"]``, ignoring anything else."""
    raw = config.get("requires")
    if not isinstance(raw, list | tuple):
        return ()
    known = {c.value: c for c in Capability}
    return tuple(known[v] for v in raw if isinstance(v, str) and v in known)


def skill_config(config: Mapping[str, object]) -> SkillConfig:
    """``config`` as a skill config; ``ValueError`` saying what is wrong."""
    try:
        return SkillConfig.model_validate_json(pydantic_core.to_json(dict(config)))
    except pydantic_core.PydanticSerializationError as exc:
        raise ValueError("invalid skill config (not JSON)") from exc
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(p) for p in e['loc']) or 'config'}: {e['msg']}" for e in exc.errors()
        )
        raise ValueError(f"invalid skill config ({problems})") from exc


def widened(before: Iterable[Capability], after: Iterable[Capability]) -> tuple[Capability, ...]:
    """Capabilities in ``after`` that ``before`` lacks, in declaration order."""
    old = set(before)
    new = set(after)
    return tuple(c for c in Capability if c in new and c not in old)


class SkillSummary(Contract):
    """What a session is told about one skill at its start."""

    name: SkillName
    description: SkillDescription
