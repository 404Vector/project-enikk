"""HTTP API."""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import FastAPI

from enikk_contracts.health import HealthResponse

if TYPE_CHECKING:
    from enikk_core.settings import CoreSettings

SERVICE_NAME = "enikk-core"


def create_app(settings: CoreSettings) -> FastAPI:
    app = FastAPI(title=SERVICE_NAME, version=settings.version)

    @app.get("/healthz", response_model=HealthResponse)
    def healthz() -> HealthResponse:
        return HealthResponse(service=SERVICE_NAME, version=settings.version)

    return app
