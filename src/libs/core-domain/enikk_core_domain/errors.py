"""Domain errors. The API layer maps each family to an ``ErrorCode``."""

from __future__ import annotations

from enikk_kernel.errors import EnikkError


class DomainError(EnikkError):
    """Root of errors raised by core business rules."""


class NotFoundError(DomainError):
    """The referenced entity does not exist (or is not visible to the caller)."""


class ConflictError(DomainError):
    """The request is valid but conflicts with the current state."""


class InvalidTransitionError(ConflictError):
    """A state machine was asked to make a transition it does not allow."""


class LeaseLostError(ConflictError):
    """The caller's lease is not (or no longer) the active lease of the task."""


class ConcurrentUpdateError(ConflictError):
    """Another writer changed the same data first. Retrying the request is safe."""


class UnknownAgentError(DomainError):
    """The requested agent definition does not exist."""


class InvalidRequestError(DomainError):
    """The request is well-formed but cannot be accepted as it is."""


class AuthenticationError(DomainError):
    """The caller could not be authenticated."""


class PermissionDeniedError(DomainError):
    """The caller is authenticated but not allowed to perform the operation."""


class DependencyUnavailableError(DomainError):
    """A required external system (database, Kubernetes API) is unavailable."""
