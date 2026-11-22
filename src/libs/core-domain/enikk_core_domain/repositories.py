"""Repositories the agents may work on (ADR-0019 §3), from ``config/github.yaml``."""

from __future__ import annotations

from typing import Annotated, Self

from pydantic import Field, model_validator

from enikk_kernel.models import FrozenModel

RepositoryName = Annotated[str, Field(pattern=r"^[A-Za-z0-9._-]{1,100}$")]


class ManagedRepository(FrozenModel):
    name: RepositoryName
    is_self: bool = Field(default=False, alias="self")
    """The platform's own repository: workflows stay off and protected paths apply."""
    workflows: bool = False
    """Development tokens may change workflow files of this repository."""

    @model_validator(mode="after")
    def _self_never_writes_workflows(self) -> Self:
        if self.is_self and self.workflows:
            raise ValueError(f"{self.name}: the platform repository cannot allow workflows")
        return self


class RepositoryPolicy(FrozenModel):
    repositories: Annotated[tuple[ManagedRepository, ...], Field(min_length=1)]

    @model_validator(mode="after")
    def _one_self_and_unique(self) -> Self:
        names = [r.name.lower() for r in self.repositories]
        if len(set(names)) != len(names):
            raise ValueError("repository names must be unique")
        if sum(r.is_self for r in self.repositories) != 1:
            raise ValueError("exactly one repository must be marked self")
        return self

    @property
    def own(self) -> ManagedRepository:
        return next(r for r in self.repositories if r.is_self)

    @property
    def any_workflows(self) -> bool:
        return any(r.workflows for r in self.repositories)

    def get(self, name: str) -> ManagedRepository | None:
        return next((r for r in self.repositories if r.name.lower() == name.lower()), None)
