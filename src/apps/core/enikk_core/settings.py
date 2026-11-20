"""enikk-core settings, read from ``ENIKK_CORE_*`` environment variables."""

from __future__ import annotations

from pydantic import Field

from enikk_kernel.settings import ServiceSettings


class CoreSettings(ServiceSettings, env_prefix="ENIKK_CORE_"):
    host: str = "0.0.0.0"  # noqa: S104 - must accept traffic from the pod network
    port: int = Field(default=8000, ge=1, le=65535)
    version: str = "dev"
    verbose: bool = False
