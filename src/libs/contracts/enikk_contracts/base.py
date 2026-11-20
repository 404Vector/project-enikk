"""Base class for every contract model."""

from __future__ import annotations

from enikk_kernel.models import FrozenModel


class Contract(FrozenModel):
    """Immutable, strict, closed model for data crossing a service boundary."""
