"""Repositories the agents may work on (ADR-0019 §3), from ``config/github.yaml``."""

from __future__ import annotations

from typing import Annotated, Self

from pydantic import Field, model_validator

from enikk_kernel.models import FrozenModel

RepositoryName = Annotated[str, Field(pattern=r"^[A-Za-z0-9._-]{1,100}$")]


class ManagedRepository(FrozenModel):
    name: RepositoryName
    is_self: bool = Field(default=False, alias="self")
    """The platform's own repository."""
    workflows: bool = False
    """Development tokens may change workflow files of this repository (ADR-0024)."""


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


SensitivePattern = Annotated[str, Field(min_length=1, max_length=300, pattern=r"^[^*?\[\]]+$")]


class SensitivePaths(FrozenModel):
    """Paths whose changes need the stronger approval (ADR-0024, I-20).

    A pattern ending in ``/`` covers everything below that directory; any other
    pattern is exactly one file.
    """

    paths: Annotated[tuple[SensitivePattern, ...], Field(min_length=1)]

    def matches(self, path: str) -> bool:
        return any(path.startswith(p) if p.endswith("/") else path == p for p in self.paths)

    def among(self, paths: list[str]) -> tuple[str, ...]:
        return tuple(p for p in paths if self.matches(p))
