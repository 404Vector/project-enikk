"""Agent definition schema (``agents/<name>.yaml``, ADR-0005, ADR-0012)."""

from __future__ import annotations

from typing import Annotated, Self

from pydantic import Field, StringConstraints, model_validator

from enikk_contracts.base import Contract
from enikk_contracts.common import AgentName, Engine

ToolName = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_.:-]{1,128}$")]
ServerName = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9-]{0,62}$")]


class PromptRef(Contract):
    """Reference to a Langfuse prompt. Exactly one of ``label`` or ``version``."""

    name: Annotated[str, StringConstraints(min_length=1, max_length=200)]
    label: Annotated[str, StringConstraints(min_length=1, max_length=100)] | None = None
    version: Annotated[int, Field(ge=1)] | None = None

    @model_validator(mode="after")
    def _exactly_one_selector(self) -> Self:
        if (self.label is None) == (self.version is None):
            raise ValueError("prompt needs exactly one of 'label' or 'version'")
        return self


class McpToolGrant(Contract):
    server: ServerName
    allow: Annotated[tuple[ToolName, ...], Field(min_length=1)]


class ToolPolicy(Contract):
    mcp: tuple[McpToolGrant, ...] = ()
    builtin: tuple[ToolName, ...] = ()

    @model_validator(mode="after")
    def _unique_servers(self) -> Self:
        servers = [grant.server for grant in self.mcp]
        if len(servers) != len(set(servers)):
            raise ValueError("each MCP server may appear only once")
        return self


class Limits(Contract):
    max_turns: Annotated[int, Field(ge=1, le=200)] = 20
    timeout_seconds: Annotated[int, Field(ge=10, le=3600)] = 600


class AgentDefinition(Contract):
    name: AgentName
    description: Annotated[str, StringConstraints(min_length=1, max_length=500)]
    engine: Engine
    prompt: PromptRef
    tools: ToolPolicy = ToolPolicy()
    delegates: tuple[AgentName, ...] = ()
    limits: Limits = Limits()

    @model_validator(mode="after")
    def _no_self_delegation(self) -> Self:
        if self.name in self.delegates:
            raise ValueError("an agent cannot delegate to itself")
        return self
