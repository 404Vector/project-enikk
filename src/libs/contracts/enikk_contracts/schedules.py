"""Schedules (ADR-0039): what the scheduler service reads and changes through core.

Scheduler → core. Every request names the task on whose behalf it is made (the caller
of the MCP tool) with that task's lease; core checks the lease and decides from the
task which agent and session ask.
"""

from __future__ import annotations

import uuid
from enum import StrEnum
from typing import Annotated, Final, Self

from pydantic import AwareDatetime, Field, StringConstraints, model_validator

from enikk_contracts.agents import ToolPolicy
from enikk_contracts.base import Contract
from enikk_contracts.common import AgentName, ConversationRef
from enikk_contracts.skills import SkillName

ScheduleName = Annotated[str, StringConstraints(pattern=r"^[a-z0-9][a-z0-9-]{0,62}$")]
CronExpression = Annotated[
    str, StringConstraints(min_length=9, max_length=100, pattern=r"^[0-9*/,\- ]+$")
]
TimeZoneName = Annotated[str, StringConstraints(min_length=1, max_length=64)]
FolderName = Annotated[
    str,
    StringConstraints(
        min_length=1, max_length=120, pattern=r"^[^/.\\][^/\\]{0,39}(/[^/.\\][^/\\]{0,39}){0,2}$"
    ),
]
"""A folder under ``Private/enikk/`` in the vault, at most three levels deep."""
Detail = Annotated[str, StringConstraints(max_length=500)]
MAX_LISTED: Final = 200


class ScheduleState(StrEnum):
    PENDING = "pending"
    """Outside the calling agent's own range; waits for the owner's approval."""
    ACTIVE = "active"
    PAUSED = "paused"


class InputKind(StrEnum):
    """Inputs registered in code. Nothing else can be given to a scheduled session."""

    TEXT = "text"
    """A fixed sentence written into the schedule."""
    DAY_CONVERSATIONS = "day-conversations"
    """The day's conversations, secrets masked. Untrusted input."""


UNTRUSTED_INPUTS: Final = frozenset({InputKind.DAY_CONVERSATIONS})


class OutputKind(StrEnum):
    """Outputs registered in code. All of them reach the owner only."""

    OWNER_DM = "owner-dm"
    THREAD = "thread"
    """The thread the schedule was created in."""
    VAULT_NOTE = "vault-note"
    """A note in a folder under ``Private/enikk/``."""
    DISCARD = "discard"


class Timing(Contract):
    """A 5-field cron expression in ``timezone``, or one time ``at``."""

    cron: CronExpression | None = None
    at: AwareDatetime | None = None
    timezone: TimeZoneName = "Asia/Seoul"

    @model_validator(mode="after")
    def _exactly_one(self) -> Self:
        if (self.cron is None) == (self.at is None):
            raise ValueError("timing needs exactly one of 'cron' or 'at'")
        return self


class ScheduleInput(Contract):
    kind: InputKind
    text: Annotated[str, StringConstraints(max_length=4000)] = ""

    @model_validator(mode="after")
    def _text_only_for_text(self) -> Self:
        if (self.kind is InputKind.TEXT) != bool(self.text.strip()):
            raise ValueError("'text' is required exactly for the text input")
        return self


class ScheduleOutput(Contract):
    kind: OutputKind
    folder: FolderName | None = None

    @model_validator(mode="after")
    def _folder_only_for_notes(self) -> Self:
        if (self.kind is OutputKind.VAULT_NOTE) != (self.folder is not None):
            raise ValueError("'folder' is required exactly for the vault-note output")
        return self


class ScheduleSpec(Contract):
    """What one schedule does. Its tools never exceed its agent's definition (I-16)."""

    description: Annotated[str, StringConstraints(min_length=1, max_length=500)]
    timing: Timing
    agent: AgentName
    skill: SkillName | None = None
    """A skill loaded into the session before it starts (ADR-0037 §3)."""
    tools: ToolPolicy = ToolPolicy(platform_defaults=False)
    input: ScheduleInput
    output: ScheduleOutput
    catchup: Annotated[int, Field(ge=0, le=14)] = 0
    """Missed runs, newest first, that are still made when the scheduler comes back."""
    retries: Annotated[int, Field(ge=0, le=5)] = 1


class Caller(Contract):
    """The task that called the scheduler's MCP tool, with its current lease."""

    task_id: uuid.UUID
    lease_id: uuid.UUID


class ScheduleQuery(Contract):
    caller: Caller


class ScheduleCreate(Contract):
    caller: Caller
    name: ScheduleName
    spec: ScheduleSpec


class ScheduleUpdate(Contract):
    """Replace the whole spec of a schedule."""

    caller: Caller
    spec: ScheduleSpec


class SchedulePause(Contract):
    caller: Caller
    paused: bool


class RunsQuery(Contract):
    caller: Caller
    limit: Annotated[int, Field(ge=1, le=100)] = 20


class ScheduleView(Contract):
    id: uuid.UUID
    name: ScheduleName
    spec: ScheduleSpec
    state: ScheduleState
    owner_agent: AgentName
    """The agent whose session created it."""
    origin: ConversationRef
    """The thread it was created in."""
    created_at: AwareDatetime
    updated_at: AwareDatetime


class ScheduleList(Contract):
    schedules: Annotated[tuple[ScheduleView, ...], Field(max_length=MAX_LISTED)]


class ScheduleDeleted(Contract):
    id: uuid.UUID


class RunStatus(StrEnum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class RunView(Contract):
    schedule_id: uuid.UUID
    slot: AwareDatetime
    """The time the run was due."""
    status: RunStatus
    task_id: uuid.UUID | None
    attempts: Annotated[int, Field(ge=0)]
    detail: Detail = ""
    started_at: AwareDatetime
    updated_at: AwareDatetime


class RunList(Contract):
    runs: Annotated[tuple[RunView, ...], Field(max_length=100)]
