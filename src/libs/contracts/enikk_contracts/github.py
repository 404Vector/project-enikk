"""GitHub through core (ADR-0019): the smoke check."""

from __future__ import annotations

from typing import Annotated

from pydantic import Field

from enikk_contracts.base import Contract

RepositoryName = Annotated[str, Field(pattern=r"^[A-Za-z0-9._-]{1,100}$")]


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
