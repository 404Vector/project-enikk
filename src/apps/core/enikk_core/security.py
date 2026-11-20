"""Caller authentication and per-endpoint role declarations (I-28)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from enikk_core_domain.auth import Principal
from enikk_core_domain.errors import AuthenticationError, PermissionDeniedError

if TYPE_CHECKING:
    from collections.abc import Callable

    from fastapi import Request

    from enikk_core_domain.auth import RoleDirectory, RoleKind
    from enikk_core_domain.ports import Authenticator

ROLE_ATTRIBUTE: Final = "__enikk_role__"


def requires[F: Callable[..., object]](kind: RoleKind) -> Callable[[F], F]:
    """Declare the role an endpoint accepts. Every ``/v1`` endpoint must declare one."""

    def mark(endpoint: F) -> F:
        setattr(endpoint, ROLE_ATTRIBUTE, kind)
        return endpoint

    return mark


def declared_role(endpoint: object) -> RoleKind | None:
    role: RoleKind | None = getattr(endpoint, ROLE_ATTRIBUTE, None)
    return role


class Gatekeeper:
    """Admits a request only if its token is valid and its role matches the endpoint's."""

    def __init__(self, authenticator: Authenticator, directory: RoleDirectory) -> None:
        self._authenticator = authenticator
        self._directory = directory

    async def admit(self, request: Request) -> Principal:
        kind = declared_role(request.scope.get("endpoint"))
        if kind is None:
            raise PermissionDeniedError("endpoint has no declared role")
        scheme, _, token = request.headers.get("Authorization", "").partition(" ")
        if scheme.lower() != "bearer" or not token.strip():
            raise AuthenticationError("missing bearer token")
        identity = await self._authenticator.authenticate(token.strip())
        role = self._directory.role_of(identity)
        if role is None or role.kind != kind:
            raise PermissionDeniedError(f"{identity} may not call this endpoint")
        return Principal(identity=identity, role=role)
