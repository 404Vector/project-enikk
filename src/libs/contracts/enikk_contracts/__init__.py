"""Shared contracts: the only types exchanged between enikk-core and its clients.

Contains data definitions only, no behaviour.
"""

from enikk_contracts.base import Contract
from enikk_contracts.health import HealthResponse

__all__ = ["Contract", "HealthResponse"]
