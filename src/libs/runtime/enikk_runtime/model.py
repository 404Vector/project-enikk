"""Values passed between the worker, the prompt store and an engine."""

from __future__ import annotations

import uuid
from datetime import datetime
from pathlib import Path
from typing import Annotated

from pydantic import Field, JsonValue, SecretStr, StringConstraints

from enikk_contracts.agents import AgentDefinition
from enikk_contracts.common import Identifier, NonEmptyText
from enikk_contracts.skills import Capability, SkillName, SkillSummary
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


class SessionWorkspace(FrozenModel):
    """The shared-volume paths one session may use (ADR-0037 §7)."""

    repository: Annotated[str, StringConstraints(min_length=1, max_length=200)]
    """``owner/name`` of the repository the snapshot comes from."""
    repo_dir: Path
    """The base repository (bare). Writable: git in the worktree records objects here."""
    snapshot_dir: Path | None
    """Read-only files of ``commit``; ``None`` until a fetch has ever succeeded."""
    commit: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{40}$")] | None
    fetched_at: datetime | None
    fresh: bool
    """Whether this preparation fetched or found a recent enough fetch."""
    worktree_dir: Path | None
    """This session's git worktree; ``None`` when it could not be made."""
    branch: Annotated[str, StringConstraints(min_length=1, max_length=200)] | None
    tmp_dir: Path
    """This session's place for untrusted material, such as files from the web."""

    @property
    def read_paths(self) -> tuple[Path, ...]:
        return () if self.snapshot_dir is None else (self.snapshot_dir,)

    @property
    def write_paths(self) -> tuple[Path, ...]:
        worktree = () if self.worktree_dir is None else (self.repo_dir, self.worktree_dir)
        return (*worktree, self.tmp_dir)


class LoadedSkill(FrozenModel):
    """The production version of a skill prompt (ADR-0037 §2)."""

    name: SkillName
    version: Annotated[int, Field(ge=1)]
    text: Annotated[str, StringConstraints(min_length=1)]
    description: Annotated[str, StringConstraints(min_length=1)]
    requires: tuple[Capability, ...] = ()


class SkillUse(FrozenModel):
    """A skill a run loaded, for its trace."""

    name: SkillName
    version: Annotated[int, Field(ge=1)]


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
    workspace: SessionWorkspace | None = None
    skills: tuple[SkillSummary, ...] | None = None
    """The skills the session may load; ``None`` when it gets no ``load_skill``."""
    mcp_token: SecretStr | None = None
    """Core's token for the definition's MCP tools (ADR-0039 §2); never shown to tools."""
    github_token: SecretStr | None = None
    """A short-lived GitHub token for the shell's git and ``enikk-github`` (ADR-0041 §1)."""


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
    skills: tuple[SkillUse, ...] = ()
    """Skills the run loaded, in order."""
