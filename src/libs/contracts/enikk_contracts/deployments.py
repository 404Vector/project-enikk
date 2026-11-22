"""Deployment results and reverts for the owner's DM (ADR-0023)."""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal

from pydantic import Field, StringConstraints

from enikk_contracts.base import Contract
from enikk_contracts.common import Identifier

Sha = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{40}$")]
Detail = Annotated[str, StringConstraints(max_length=500)]


class PullLink(Contract):
    number: Annotated[int, Field(ge=1)]
    title: Annotated[str, StringConstraints(max_length=300)]
    url: Annotated[str, StringConstraints(min_length=1, max_length=500)]


class AppResult(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    REPLACED = "replaced"
    """A newer deployment of the same app started before this one settled."""


class AppDeployment(Contract):
    app: Annotated[str, StringConstraints(min_length=1, max_length=100)]
    result: AppResult
    detail: Detail = ""


class DeploymentFinished(Contract):
    """Every app that deployed ``commit`` has settled."""

    kind: Literal["deployment-finished"] = "deployment-finished"
    repository: Annotated[str, StringConstraints(min_length=3, max_length=200)]
    commit: Sha
    pull: PullLink | None = None
    succeeded: bool
    rollback_of: Sha | None = None
    """Set when this deployment is the revert of that commit: a rollback result."""
    apps: Annotated[tuple[AppDeployment, ...], Field(min_length=1, max_length=20)]


class BuildFailed(Contract):
    """The image build of a commit on main failed, so nothing was deployed."""

    kind: Literal["build-failed"] = "build-failed"
    repository: Annotated[str, StringConstraints(min_length=3, max_length=200)]
    commit: Sha
    pull: PullLink | None = None
    run_url: Annotated[str, StringConstraints(min_length=1, max_length=500)]
    conclusion: Annotated[str, StringConstraints(min_length=1, max_length=50)]
    rollback_of: Sha | None = None


class RevertRequest(Contract):
    """The owner pressed [되돌리기] on a failed deployment or build of ``commit``."""

    commit: Sha
    requested_by: Identifier


class RevertOutcome(StrEnum):
    OPENED = "opened"
    EXISTING = "existing"
    """A revert PR for this commit is already open."""
    REFUSED = "refused"
    """It cannot be reverted automatically; ``detail`` says why."""


class RevertResult(Contract):
    outcome: RevertOutcome
    pull: PullLink | None = None
    detail: Annotated[str, StringConstraints(max_length=3_000)] = ""
