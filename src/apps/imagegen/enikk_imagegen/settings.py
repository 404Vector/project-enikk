"""Settings, read from ``ENIKK_IMAGEGEN_*`` environment variables."""

from __future__ import annotations

from pathlib import Path

from pydantic import Field

from enikk_core_client.tokens import PROJECTED_TOKEN_FILE
from enikk_kernel.settings import ServiceSettings

DEFAULT_PRICES: dict[str, dict[str, float]] = {
    "gemini-3.1-flash-image": {"512": 0.045, "1K": 0.067, "2K": 0.101, "4K": 0.151},
    "gemini-3-pro-image": {"1K": 0.134, "2K": 0.134, "4K": 0.24},
    "gemini-2.5-flash-image": {"1K": 0.039},
}
"""Estimated USD per image by model and size (ADR-0043). Only for traces and replies;
Google bills by tokens. They are also what the daily budget counts (ADR-0046); a
model and size missing here cannot be used."""


class ImagegenSettings(ServiceSettings, env_prefix="ENIKK_IMAGEGEN_"):
    core_url: str = "http://enikk-core.enikk.svc"
    token_file: Path = PROJECTED_TOKEN_FILE
    langfuse_url: str = "https://jp.cloud.langfuse.com"
    tracing: bool = True
    """Connect to Langfuse: one trace per image request, and the styles (style-*)."""
    gemini_url: str = "https://generativelanguage.googleapis.com/v1beta"
    default_model: str = "gemini-3.1-flash-image"
    models: tuple[str, ...] = ("gemini-3.1-flash-image", "gemini-3-pro-image")
    """Models a call may ask for. The first that is not the default costs more."""
    prices: dict[str, dict[str, float]] = Field(default_factory=lambda: dict(DEFAULT_PRICES))
    default_budget_usd: float = Field(default=1.0, ge=0)
    """Estimated USD per day while the Langfuse setting ``config-imagegen`` has no
    production version, or when there is no Langfuse connection (ADR-0046)."""
    timezone: str = "Asia/Seoul"
    """Where a day starts and ends for the daily budget."""
    request_seconds: float = Field(default=180.0, gt=0)
    workspace: Path = Path("/data")
    """The runtime's shared volume (ADR-0037 §7): images go to ``tmp/<session>/images``,
    style references live in ``styles/<style>``."""
    host: str = "0.0.0.0"  # noqa: S104 - reached only through its Service in the cluster.
    port: int = Field(default=8080, ge=1, le=65535)
    allowed_hosts: tuple[str, ...] = (
        "enikk-imagegen",
        "enikk-imagegen.enikk.svc",
        "enikk-imagegen.enikk.svc.cluster.local",
        # agentgateway passes on the Host its caller used (ADR-0039 S3).
        "enikk-agentgateway",
        "enikk-agentgateway.enikk.svc",
        "enikk-agentgateway.enikk.svc.cluster.local",
    )
    """Host header values the MCP endpoint accepts, with any port."""
    verbose: bool = False
