"""Agent definition schema (``agents/<name>.yaml``, ADR-0005, ADR-0012)."""

from __future__ import annotations

from enum import StrEnum
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


BUILTIN_TOOLS: frozenset[str] = frozenset(
    {
        # files
        "Read",
        "Write",
        "Edit",
        "NotebookEdit",
        # search
        "Glob",
        "Grep",
        # commands, including background ones
        "Bash",
        "Monitor",
        "TaskStop",
        # web; its content is untrusted input
        "WebSearch",
        "WebFetch",
        # work tracking and sub-agents
        "TaskCreate",
        "TaskGet",
        "TaskList",
        "TaskUpdate",
        "Task",
    }
)
"""Built-in tools of Claude Code (2.1.330) a role may be granted (ADR-0033). They run in
the engine's sandbox. Others (scheduling, worktrees, skills, workflows) are not."""


class PlatformTool(StrEnum):
    """Platform tools a role must be granted by name (ADR-0032). Others go to every role."""

    PROMPT_CANDIDATES = "prompt-candidates"
    """Create a candidate version of a role's prompt and ask the owner to promote it."""
    CONVERSATION_DIGEST = "conversation-digest"
    """Ask core to make (or remake) one day's conversation summary (ADR-0035)."""
    GITHUB = "github"
    """A short-lived GitHub token for the platform repository in the shell: push agent
    branches, open and close pull requests (ADR-0041 §1)."""


class ToolPolicy(Contract):
    mcp: tuple[McpToolGrant, ...] = ()
    builtin: tuple[ToolName, ...] = ()
    platform: tuple[PlatformTool, ...] = ()
    platform_defaults: bool = True
    """Whether the role gets the platform tools every role gets by default (memory and
    diagnostics). False gives a role none of them (the summarizer, ADR-0035)."""

    @model_validator(mode="after")
    def _unique_servers(self) -> Self:
        servers = [grant.server for grant in self.mcp]
        if len(servers) != len(set(servers)):
            raise ValueError("each MCP server may appear only once")
        return self


class Limits(Contract):
    max_turns: Annotated[int, Field(ge=1, le=200)] = 20
    timeout_seconds: Annotated[int, Field(ge=10, le=3600)] = 600


class Execution(StrEnum):
    """Where an agent's tasks run (ADR-0021)."""

    SHARED = "shared"
    """The engine's long-running runtime Deployment."""
    ISOLATED = "isolated"
    """A fresh Job per task in the ``enikk-dev`` namespace. Never the shared runtime."""


class AgentDefinition(Contract):
    name: AgentName
    aliases: Annotated[tuple[AgentName, ...], Field(max_length=10)] = ()
    """Former names of this agent. Sessions and tasks recorded under one of them run as
    this agent (ADR-0037 5b: ``assistant`` became ``enikk``)."""
    description: Annotated[str, StringConstraints(min_length=1, max_length=500)]
    engine: Engine
    execution: Execution = Execution.SHARED
    prompt: PromptRef
    tools: ToolPolicy = ToolPolicy()
    limits: Limits = Limits()

    @model_validator(mode="after")
    def _distinct_aliases(self) -> Self:
        if self.name in self.aliases or len(set(self.aliases)) != len(self.aliases):
            raise ValueError("aliases must differ from the name and from each other")
        return self

    @model_validator(mode="after")
    def _known_builtin_tools(self) -> Self:
        unknown = sorted(set(self.tools.builtin) - BUILTIN_TOOLS)
        if unknown:
            raise ValueError(f"built-in tools {unknown} are not ones a role may have (ADR-0033)")
        return self
