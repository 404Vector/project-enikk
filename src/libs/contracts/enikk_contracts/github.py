"""GitHub through core (ADR-0019): the smoke check, and dev agent tokens."""

from __future__ import annotations

import uuid
from typing import Annotated

from pydantic import AwareDatetime, Field

from enikk_contracts.base import Contract
from enikk_contracts.credentials import CredentialValue

RepositoryName = Annotated[str, Field(pattern=r"^[A-Za-z0-9._-]{1,100}$")]


class GitHubTokenRequest(Contract):
    """An isolated worker asks for the token of the task it holds the lease of."""

    task_id: uuid.UUID
    lease_id: uuid.UUID
    repository: RepositoryName | None = None
    """A managed repository; the platform's own repository when omitted."""


class GitHubToken(Contract):
    """A development token for one repository (I-32). Revealed only in JSON."""

    token: CredentialValue
    owner: str
    repository: RepositoryName
    expires_at: AwareDatetime
    workflows: bool
    """Whether the token may change workflow files (never for the platform itself)."""
    author_name: str
    author_email: str


class RepositoryCheck(Contract):
    name: str
    reachable: bool


class PermissionGap(Contract):
    permission: str
    required: str
    granted: str | None
    """``None`` when the installation does not grant the permission at all."""


class GitHubHealth(Contract):
    configured: bool
    """App and installation IDs are set."""
    key_registered: bool
    detail: str | None = None
    """Why the check could not finish, if it could not."""
    repositories: tuple[RepositoryCheck, ...] = ()
    gaps: tuple[PermissionGap, ...] = ()

    @property
    def ok(self) -> bool:
        return (
            self.configured
            and self.key_registered
            and self.detail is None
            and not self.gaps
            and all(r.reachable for r in self.repositories)
        )
