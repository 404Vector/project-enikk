"""Client-side errors, one per ``ErrorCode``."""

from __future__ import annotations

from enikk_contracts.errors import ErrorCode
from enikk_kernel.errors import EnikkError


class CoreApiError(EnikkError):
    code: ErrorCode = ErrorCode.INTERNAL

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class CoreUnauthenticatedError(CoreApiError):
    code = ErrorCode.UNAUTHENTICATED


class CoreForbiddenError(CoreApiError):
    code = ErrorCode.FORBIDDEN


class CoreNotFoundError(CoreApiError):
    code = ErrorCode.NOT_FOUND


class CoreConflictError(CoreApiError):
    code = ErrorCode.CONFLICT


class CoreInvalidRequestError(CoreApiError):
    code = ErrorCode.INVALID_REQUEST


class CoreUnavailableError(CoreApiError):
    """core (or one of its dependencies) cannot be reached. Retrying later is safe."""

    code = ErrorCode.UNAVAILABLE


ERRORS_BY_CODE: dict[ErrorCode, type[CoreApiError]] = {
    cls.code: cls
    for cls in (
        CoreApiError,
        CoreUnauthenticatedError,
        CoreForbiddenError,
        CoreNotFoundError,
        CoreConflictError,
        CoreInvalidRequestError,
        CoreUnavailableError,
    )
}
