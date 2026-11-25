"""Read-only diagnostics: what enikk can see of its own state (ADR-0029)."""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Annotated

from pydantic import Field, StringConstraints

from enikk_contracts.base import Contract
from enikk_contracts.tasks import TaskStatus

Short = Annotated[str, StringConstraints(max_length=2_000)]


class RecentTasksQuery(Contract):
    lease_id: uuid.UUID
    status: TaskStatus | None = None
    role: Annotated[str, StringConstraints(max_length=100)] | None = None
    hours: Annotated[int, Field(ge=1, le=168)] = 24
    limit: Annotated[int, Field(ge=1, le=30)] = 15


class TaskSummary(Contract):
    task_id: uuid.UUID
    role: str
    status: TaskStatus
    attempt: int
    failure: Short | None = None
    created_at: datetime
    updated_at: datetime
    thread: str
    """Connector, channel and thread of the conversation."""


class RecentTasks(Contract):
    tasks: tuple[TaskSummary, ...]


class TaskDetailQuery(Contract):
    lease_id: uuid.UUID
    task_id: uuid.UUID


class TaskDetail(Contract):
    summary: TaskSummary
    input_head: Short
    output_head: Short | None = None
    previous: TaskSummary | None = None
    """The task before it in the same session."""


class DeploymentsQuery(Contract):
    lease_id: uuid.UUID
    limit: Annotated[int, Field(ge=1, le=20)] = 8


class DeploymentEvent(Contract):
    at: datetime
    kind: str
    commit: str
    succeeded: bool
    summary: Short


class AppState(Contract):
    app: str
    image_tag: str | None
    sync: str | None
    health: str
    message: Short = ""


class Deployments(Contract):
    events: tuple[DeploymentEvent, ...]
    apps: tuple[AppState, ...]


class Service(StrEnum):
    CORE = "core"
    RUNTIME_CLAUDE = "runtime-claude"
    CONNECTOR_SLACK = "connector-slack"
    SCHEDULER = "scheduler"
    IMAGEGEN = "imagegen"
    AGENTGATEWAY = "agentgateway"


class LogsQuery(Contract):
    lease_id: uuid.UUID
    service: Service
    minutes: Annotated[int, Field(ge=1, le=180)] = 30
    contains: Annotated[str, StringConstraints(min_length=1, max_length=100)] | None = None


class LogLines(Contract):
    pods: tuple[str, ...]
    lines: tuple[Annotated[str, StringConstraints(max_length=2_000)], ...]
    truncated: bool = False


class PullsQuery(Contract):
    lease_id: uuid.UUID


class PullStatus(Contract):
    number: int
    title: Short
    url: str
    branch: str
    head_sha: str
    checks: Short
    approval: Short


class Pulls(Contract):
    pulls: tuple[PullStatus, ...]
