"""Identifier source port. Code that creates identifiers receives an ``IdGenerator``."""

from __future__ import annotations

import uuid
from typing import Protocol


class IdGenerator(Protocol):
    def new(self) -> uuid.UUID: ...


class RandomIdGenerator:
    def new(self) -> uuid.UUID:
        return uuid.uuid4()
