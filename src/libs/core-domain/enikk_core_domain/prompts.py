"""Prompt candidates and their promotion (ADR-0032): the record, the rules, the port.

Langfuse is the source of prompts (I-13, I-24). Core writes a candidate version there
and moves ``production`` only when the owner promotes or reverts it in Slack (I-36).
"""

from __future__ import annotations

import difflib
import uuid
from typing import TYPE_CHECKING, Annotated, Final, Literal, Protocol, Self

import pydantic_core
from pydantic import AwareDatetime, Field, JsonValue

from enikk_contracts.common import Identifier
from enikk_contracts.prompts import PromotionStatus
from enikk_contracts.skills import (
    Capability,
    declared_requires,
    is_skill,
    skill_config,
    valid_skill_name,
    widened,
)
from enikk_contracts.styles import is_style, style_config, valid_style_name
from enikk_contracts.tool_configs import is_tool_config, tool_config
from enikk_core_domain.errors import InvalidRequestError, InvalidTransitionError
from enikk_core_domain.model import Entity
from enikk_core_domain.redaction import looks_secret
from enikk_kernel.models import FrozenModel

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import datetime

    from enikk_contracts.agents import AgentDefinition
    from enikk_contracts.prompts import PromptConfigChange

PRODUCTION: Final = "production"
CANDIDATE: Final = "candidate"
MODEL_KEY: Final = "model"


class StoredPrompt(FrozenModel):
    """One version of a prompt as Langfuse stores it."""

    name: str
    version: Annotated[int, Field(ge=1)]
    type: Literal["text", "chat"]
    text: str | None
    """The prompt of a text prompt; ``None`` for chat prompts."""
    config: dict[str, JsonValue] = Field(default_factory=dict)
    labels: tuple[str, ...] = ()


class PromptRegistry(Protocol):
    """Prompt versions and labels in Langfuse. Implemented by the langfuse adapter."""

    async def get(
        self, name: str, *, label: str | None = None, version: int | None = None
    ) -> StoredPrompt | None:
        """The version with ``label`` or ``version``; ``None`` when there is none."""
        ...

    async def create_candidate(
        self, name: str, text: str, config: dict[str, JsonValue], message: str
    ) -> StoredPrompt:
        """Create a new text version labelled ``candidate`` and nothing else."""
        ...

    async def set_production(self, name: str, version: int) -> None:
        """Label ``version`` production. Langfuse removes it from the other versions."""
        ...


class PromptPromotion(Entity):
    """The owner's decision on one candidate version (ADR-0032 §5)."""

    id: uuid.UUID
    prompt: Annotated[str, Field(min_length=1, max_length=200)]
    production_version: Annotated[int, Field(ge=1)] | None
    """``production`` when the candidate was made."""
    candidate_version: Annotated[int, Field(ge=1)]
    reason: Annotated[str, Field(min_length=1, max_length=500)]
    task_id: uuid.UUID
    """The task that asked for it; its thread hears the outcome."""
    status: PromotionStatus = PromotionStatus.REQUESTED
    created_at: AwareDatetime
    decided_at: AwareDatetime | None = None
    decided_by: Identifier | None = None

    def decide(self, status: PromotionStatus, now: datetime, by: str) -> Self:
        allowed = {PromotionStatus.PROMOTED, PromotionStatus.REJECTED, PromotionStatus.STALE}
        if self.status is not PromotionStatus.REQUESTED or status not in allowed:
            raise InvalidTransitionError(f"promotion {self.id} is {self.status}, not {status}")
        return self._evolve(status=status, decided_at=now, decided_by=by)

    def revert(self, now: datetime, by: str) -> Self:
        if self.status is not PromotionStatus.PROMOTED or self.production_version is None:
            raise InvalidTransitionError(f"promotion {self.id} cannot be reverted")
        return self._evolve(status=PromotionStatus.REVERTED, decided_at=now, decided_by=by)


class Candidate(FrozenModel):
    """A checked candidate, ready to be written to Langfuse."""

    text: str
    config: dict[str, JsonValue]
    text_diff: str
    config_diff: str
    requires_added: tuple[Capability, ...] = ()
    """Skills only: actions the candidate relies on that production does not."""


def roles_using(prompt: str, definitions: Sequence[AgentDefinition]) -> tuple[str, ...]:
    """Roles whose definition uses ``prompt``, sorted."""
    return tuple(sorted(d.name for d in definitions if d.prompt.name == prompt))


def check_candidate(
    prompt: str,
    production: StoredPrompt | None,
    text: str,
    change: PromptConfigChange | None,
) -> Candidate:
    """The candidate's text and config, or ``InvalidRequestError`` saying what is wrong."""
    skill = is_skill(prompt)
    if skill and not valid_skill_name(prompt):
        raise InvalidRequestError(
            f"'{prompt}' is not a valid skill name: skill- then lowercase letters, digits, '-'"
        )
    style = is_style(prompt)
    if style and not valid_style_name(prompt):
        raise InvalidRequestError(
            f"'{prompt}' is not a valid style name: style- then lowercase letters, digits, '-'"
        )
    if not skill and change is not None and change.requires is not None:
        raise InvalidRequestError("requires is only for skill prompts (skill-*)")
    setting = is_tool_config(prompt)
    if not setting and change is not None and change.values is not None:
        raise InvalidRequestError("values is only for tool settings (config-*)")
    if setting and change is not None and (change.model is not None or change.effort is not None):
        raise InvalidRequestError("tool settings (config-*) take values, not model or effort")
    if not skill and not style and change is not None and change.description is not None:
        raise InvalidRequestError(
            "description is only for skill prompts (skill-*) and style prompts (style-*)"
        )
    if not text.strip():
        raise InvalidRequestError("the prompt text is empty")
    if looks_secret(text):
        raise InvalidRequestError("the prompt text looks like it contains a secret")
    if production is not None and production.type != "text":
        raise InvalidRequestError(
            f"production of '{prompt}' is a {production.type} prompt; roles need text prompts"
        )
    config: dict[str, JsonValue] = dict(production.config) if production is not None else {}
    if change is not None:
        config.update(change.model_dump(mode="json", exclude_none=True, exclude={"values"}))
        config.update(change.values or {})
    if setting:
        try:
            tool_config(prompt, config)
        except ValueError as exc:
            raise InvalidRequestError(str(exc)) from exc
    model = config.get(MODEL_KEY)
    if not setting and (not isinstance(model, str) or not model.strip()):
        raise InvalidRequestError(f'the config of the candidate has no "{MODEL_KEY}"')
    requires_added: tuple[Capability, ...] = ()
    if skill:
        try:
            checked = skill_config(config)
        except ValueError as exc:
            raise InvalidRequestError(str(exc)) from exc
        before_requires = declared_requires(production.config) if production is not None else ()
        requires_added = widened(before_requires, checked.requires)
    if style:
        try:
            style_config(config)
        except ValueError as exc:
            raise InvalidRequestError(str(exc)) from exc
    before_text = (production.text or "") if production is not None else ""
    before_config = production.config if production is not None else {}
    if production is not None and before_text == text and before_config == config:
        raise InvalidRequestError("the candidate is the same as production; nothing to change")
    before = f"production v{production.version}" if production is not None else "(none)"
    return Candidate(
        text=text,
        config=config,
        text_diff=_diff(before_text, text, before),
        config_diff=_diff(_json(before_config), _json(config), before),
        requires_added=requires_added,
    )


def _diff(before: str, after: str, label: str) -> str:
    lines = difflib.unified_diff(
        before.splitlines(), after.splitlines(), fromfile=label, tofile="candidate", lineterm=""
    )
    return "\n".join(lines)


def _json(config: dict[str, JsonValue]) -> str:
    if not config:
        return ""
    ordered = {key: config[key] for key in sorted(config)}
    return pydantic_core.to_json(ordered, indent=2).decode()
