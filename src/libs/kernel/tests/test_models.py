from __future__ import annotations

import pytest
from pydantic import ValidationError

from enikk_kernel.models import FrozenModel
from enikk_kernel.settings import ServiceSettings, SettingsModel


class _Point(FrozenModel):
    x: int


def test_frozen_model_rejects_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        _Point.model_validate({"x": 1, "y": 2})


def test_frozen_model_is_strict() -> None:
    with pytest.raises(ValidationError):
        _Point.model_validate({"x": "1"})


def test_frozen_model_is_immutable() -> None:
    point = _Point(x=1)
    with pytest.raises(ValidationError):
        point.x = 2  # type: ignore[misc]


class _Nested(SettingsModel):
    host: str
    port: int


class _Settings(ServiceSettings, env_prefix="ENIKK_TEST_"):
    name: str
    nested: _Nested


def test_settings_read_nested_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENIKK_TEST_NAME", "core")
    monkeypatch.setenv("ENIKK_TEST_NESTED__HOST", "localhost")
    monkeypatch.setenv("ENIKK_TEST_NESTED__PORT", "5432")
    settings = _Settings()  # type: ignore[call-arg]
    assert settings.name == "core"
    assert settings.nested == _Nested(host="localhost", port=5432)
