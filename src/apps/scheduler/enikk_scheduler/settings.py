"""Settings, read from ``ENIKK_SCHEDULER_*`` environment variables."""

from __future__ import annotations

from pathlib import Path

from pydantic import Field

from enikk_core_client.tokens import PROJECTED_TOKEN_FILE
from enikk_kernel.settings import ServiceSettings


class SchedulerSettings(ServiceSettings, env_prefix="ENIKK_SCHEDULER_"):
    core_url: str = "http://enikk-core.enikk.svc"
    token_file: Path = PROJECTED_TOKEN_FILE
    host: str = "0.0.0.0"  # noqa: S104 - reached only through its Service in the cluster.
    port: int = Field(default=8080, ge=1, le=65535)
    allowed_hosts: tuple[str, ...] = (
        "enikk-scheduler",
        "enikk-scheduler.enikk.svc",
        "enikk-scheduler.enikk.svc.cluster.local",
    )
    """Host header values the MCP endpoint accepts, with any port."""
    verbose: bool = False
