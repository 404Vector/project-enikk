from __future__ import annotations

from fastapi.testclient import TestClient

from enikk_contracts.health import HealthResponse
from enikk_core.api import create_app
from enikk_core.settings import CoreSettings


def test_healthz_returns_contract() -> None:
    client = TestClient(create_app(CoreSettings(version="sha-abc")))
    response = client.get("/healthz")
    assert response.status_code == 200
    body = HealthResponse.model_validate_json(response.content)
    assert body == HealthResponse(service="enikk-core", version="sha-abc")
