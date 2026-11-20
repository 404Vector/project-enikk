from __future__ import annotations

import pytest
from pydantic import ValidationError

from enikk_contracts import Contract


class _Ping(Contract):
    message: str


def test_contract_round_trips_through_json() -> None:
    ping = _Ping(message="hello")
    assert _Ping.model_validate_json(ping.model_dump_json()) == ping


def test_contract_rejects_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        _Ping.model_validate_json('{"message": "hello", "extra": 1}')
