"""Error body returned by every failed core API call."""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated

from pydantic import StringConstraints

from enikk_contracts.base import Contract


class ErrorCode(StrEnum):
    UNAUTHENTICATED = "unauthenticated"
    FORBIDDEN = "forbidden"
    NOT_FOUND = "not_found"
    CONFLICT = "conflict"
    INVALID_REQUEST = "invalid_request"
    UNAVAILABLE = "unavailable"
    INTERNAL = "internal"


class ErrorResponse(Contract):
    code: ErrorCode
    message: Annotated[str, StringConstraints(max_length=2_000)]
