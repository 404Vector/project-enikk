"""Health check contract."""

from __future__ import annotations

from typing import Literal

from enikk_contracts.base import Contract


class HealthResponse(Contract):
    status: Literal["ok"] = "ok"
    service: str
    version: str
