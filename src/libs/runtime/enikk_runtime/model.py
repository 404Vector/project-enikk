"""Values passed between the worker, the prompt store and an engine."""

from __future__ import annotations

import uuid
from typing import Annotated

from pydantic import Field, JsonValue, StringConstraints

from enikk_contracts.agents import AgentDefinition
from enikk_contracts.common import Identifier, NonEmptyText
from enikk_kernel.models import FrozenModel

ModelId = Annotated[str, StringConstraints(min_length=1, max_length=200)]


class ResolvedPrompt(FrozenModel):
    """A prompt fetched from the prompt store (I-13, I-16)."""

    name: Annotated[str, StringConstraints(min_length=1, max_length=200)]
    version: Annotated[int, Field(ge=1)]
    text: Annotated[str, StringConstraints(min_length=1)]
    model: ModelId
    parameters: dict[str, JsonValue] = Field(default_factory=dict)
    """Generation parameters from the prompt config, other than the model."""


class EngineJob(FrozenModel):
    """Everything an engine needs to run one task."""

    task_id: uuid.UUID
    lease_id: uuid.UUID
    attempt: Annotated[int, Field(ge=1)]
    session_id: uuid.UUID
    engine_session_id: Identifier | None
    definition: AgentDefinition
    prompt: ResolvedPrompt
    input: NonEmptyText


TokenCount = Annotated[int, Field(ge=0)]


class TokenUsage(FrozenModel):
    input_tokens: TokenCount = 0
    output_tokens: TokenCount = 0
    cache_read_tokens: TokenCount = 0
    cache_creation_tokens: TokenCount = 0


class EngineResult(FrozenModel):
    text: NonEmptyText
    engine_session_id: Identifier | None = None
    model: ModelId | None = None
    """The model that actually answered, when the engine reports it."""
    usage: TokenUsage | None = None
