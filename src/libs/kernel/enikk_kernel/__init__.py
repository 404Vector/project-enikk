"""Foundation package. Depends on no other project package."""

from enikk_kernel.clock import Clock, SystemClock
from enikk_kernel.errors import (
    ConfigurationError,
    EnikkError,
    ExternalCommandError,
    ToolNotFoundError,
)
from enikk_kernel.ids import IdGenerator, RandomIdGenerator
from enikk_kernel.models import FrozenModel
from enikk_kernel.process import ProcessResult, ProcessRunner, SubprocessRunner
from enikk_kernel.settings import ServiceSettings, SettingsModel

__all__ = [
    "Clock",
    "ConfigurationError",
    "EnikkError",
    "ExternalCommandError",
    "FrozenModel",
    "IdGenerator",
    "ProcessResult",
    "ProcessRunner",
    "RandomIdGenerator",
    "ServiceSettings",
    "SettingsModel",
    "SubprocessRunner",
    "SystemClock",
    "ToolNotFoundError",
]
