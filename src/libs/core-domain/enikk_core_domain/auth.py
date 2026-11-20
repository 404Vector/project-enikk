"""Caller identities and roles (ADR-0011 §5)."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field, StringConstraints

from enikk_contracts.common import ConnectorKind, Engine
from enikk_kernel.models import FrozenModel

K8sName = Annotated[str, StringConstraints(pattern=r"^[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?$")]


class ServiceIdentity(FrozenModel):
    """A Kubernetes ServiceAccount verified by the authenticator."""

    namespace: K8sName
    name: K8sName

    def __str__(self) -> str:
        return f"{self.namespace}/{self.name}"


class ConnectorRole(FrozenModel):
    kind: Literal["connector"] = "connector"
    connector: ConnectorKind


class RuntimeRole(FrozenModel):
    kind: Literal["runtime"] = "runtime"
    engine: Engine


class DevAgentRole(FrozenModel):
    kind: Literal["dev-agent"] = "dev-agent"


Role = Annotated[ConnectorRole | RuntimeRole | DevAgentRole, Field(discriminator="kind")]
RoleKind = Literal["connector", "runtime", "dev-agent"]


class Principal(FrozenModel):
    identity: ServiceIdentity
    role: Role


class RoleBinding(FrozenModel):
    identity: ServiceIdentity
    role: Role


class RoleDirectory:
    """Maps verified identities to roles. Unknown identities have no role."""

    def __init__(self, bindings: tuple[RoleBinding, ...]) -> None:
        self._roles: dict[ServiceIdentity, Role] = {}
        for binding in bindings:
            if binding.identity in self._roles:
                raise ValueError(f"duplicate role binding for {binding.identity}")
            self._roles[binding.identity] = binding.role

    def role_of(self, identity: ServiceIdentity) -> Role | None:
        return self._roles.get(identity)
