"""Prompt candidates and their promotion by the owner in Slack (ADR-0032).

Runtime → core: create a candidate. Core → connector: the owner's promotion request.
Connector → core: the owner's button press.
"""

from __future__ import annotations

import uuid
from enum import StrEnum
from typing import Annotated, Final, Literal, Self

from pydantic import Field, JsonValue, StringConstraints, model_validator

from enikk_contracts.base import Contract
from enikk_contracts.common import AgentName, ConversationRef, Identifier

MAX_PROMPT_CHARS: Final = 100_000
MAX_DIFF_CHARS: Final = 200_000

PromptName = Annotated[str, StringConstraints(min_length=1, max_length=200)]
Version = Annotated[int, Field(ge=1)]
Reason = Annotated[str, StringConstraints(min_length=1, max_length=500, strip_whitespace=True)]
ModelId = Annotated[str, StringConstraints(min_length=1, max_length=100)]
Effort = Annotated[str, StringConstraints(min_length=1, max_length=20)]


class PromptConfigChange(Contract):
    """Config values to change. Each one replaces the current production value."""

    model: ModelId | None = None
    effort: Effort | None = None


class PromptCandidateRequest(Contract):
    """Create a candidate version of a role's prompt. It carries no label: the candidate
    is created with ``candidate`` alone, and only the owner moves ``production``."""

    lease_id: uuid.UUID
    prompt: PromptName
    text: Annotated[str, StringConstraints(min_length=1, max_length=MAX_PROMPT_CHARS)]
    reason: Reason
    config: PromptConfigChange | None = None


class PromptReadRequest(Contract):
    """Read one version of a role's prompt through core (ADR-0032 §8). Read-only.
    Neither ``label`` nor ``version``: ``production``."""

    lease_id: uuid.UUID
    prompt: PromptName
    label: Annotated[str, StringConstraints(min_length=1, max_length=100)] | None = None
    version: Version | None = None

    @model_validator(mode="after")
    def _one_selector(self) -> Self:
        if self.label is not None and self.version is not None:
            raise ValueError("give label or version, not both")
        return self


class PromptRead(Contract):
    prompt: PromptName
    version: Version
    labels: Annotated[tuple[str, ...], Field(max_length=50)] = ()
    type: Literal["text", "chat"]
    text: Annotated[str, StringConstraints(max_length=MAX_PROMPT_CHARS)] | None
    """The prompt of a text prompt; ``None`` for a chat prompt."""
    config: dict[str, JsonValue] = Field(default_factory=dict)


class PromptCandidateCreated(Contract):
    promotion_id: uuid.UUID
    prompt: PromptName
    candidate_version: Version
    production_version: Version | None
    """``production`` when the candidate was made; ``None`` if the prompt had none."""


class PromotionRequested(Contract):
    """The owner's DM: promote ``candidate_version`` of ``prompt`` to production?"""

    kind: Literal["prompt-promotion-requested"] = "prompt-promotion-requested"
    promotion_id: uuid.UUID
    prompt: PromptName
    roles: Annotated[tuple[AgentName, ...], Field(max_length=50)] = ()
    """Roles whose definitions use this prompt."""
    production_version: Version | None
    candidate_version: Version
    reason: Reason
    text_diff: Annotated[str, StringConstraints(max_length=MAX_DIFF_CHARS)] = ""
    config_diff: Annotated[str, StringConstraints(max_length=20_000)] = ""
    origin: ConversationRef | None = None
    """The thread where the owner asked for the change."""


class PromotionAction(StrEnum):
    PROMOTE = "promote"
    REJECT = "reject"
    REVERT = "revert"
    """Move production back to the version it had before a promotion."""


class PromotionStatus(StrEnum):
    REQUESTED = "requested"
    PROMOTED = "promoted"
    REJECTED = "rejected"
    STALE = "stale"
    """Production changed after the request; nothing was promoted."""
    REVERTED = "reverted"


class PromotionDecision(Contract):
    """The owner pressed a button. The connector has checked who pressed it (I-01)."""

    action: PromotionAction
    decided_by: Identifier


class PromotionResult(Contract):
    status: PromotionStatus
    changed: bool
    """Whether this press changed Langfuse or the request; false for a repeated press."""
    prompt: PromptName
    production_version: Version | None
    """``production`` before the promotion."""
    candidate_version: Version
    detail: Annotated[str, StringConstraints(max_length=1_000)] = ""


class PromotionPosted(Contract):
    """The promotion request is in the owner's DM; ``link`` is its permalink, if any."""

    link: Annotated[str, StringConstraints(min_length=1, max_length=500)] | None = None


class PromotionLinked(Contract):
    told_origin: bool
