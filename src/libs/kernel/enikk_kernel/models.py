"""Base model for value objects and contracts."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class FrozenModel(BaseModel):
    """Immutable, strictly validated model that rejects unknown fields."""

    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")
