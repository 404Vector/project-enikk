"""Caller authentication and per-endpoint role declarations (I-28)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from enikk_core_domain.errors import AuthenticationError, PermissionDeniedError

if TYPE_CHECKING:
    from collections.abc import Callable

    from fastapi import Request

    from enikk_core_domain.auth import Principal, RoleDirectory, RoleKind
    from enikk_core_domain.ports import Authenticator

ROLE_ATTRIBUTE: Final = "__enikk_roles__"


def requires[F: Callable[..., object]](*kinds: RoleKind) -> Callable[[F], F]:
    """Declare the roles an endpoint accepts. Every ``/v1`` endpoint must declare one."""
    if not kinds:
        raise ValueError("declare at least one role")

    def mark(endpoint: F) -> F:
        setattr(endpoint, ROLE_ATTRIBUTE, frozenset(kinds))
        return endpoint

    return mark


def declared_roles(endpoint: object) -> frozenset[RoleKind] | None:
    roles: frozenset[RoleKind] | None = getattr(endpoint, ROLE_ATTRIBUTE, None)
    return roles


class Gatekeeper:
    """Admits a request only if its token is valid and its role is one the endpoint accepts."""

    def __init__(self, authenticator: Authenticator, directory: RoleDirectory) -> None:
        self._authenticator = authenticator
        self._directory = directory

    async def admit(self, request: Request) -> Principal:
        kinds = declared_roles(request.scope.get("endpoint"))
        if not kinds:
            raise PermissionDeniedError("endpoint has no declared role")
        scheme, _, token = request.headers.get("Authorization", "").partition(" ")
        if scheme.lower() != "bearer" or not token.strip():
            raise AuthenticationError("missing bearer token")
        identity = await self._authenticator.authenticate(token.strip())
        binding = self._directory.binding_of(identity)
        if binding is None or binding.role.kind not in kinds:
            raise PermissionDeniedError(f"{identity} may not call this endpoint")
        return binding.principal()
