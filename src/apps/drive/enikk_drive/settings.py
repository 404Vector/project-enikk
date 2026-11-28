"""Settings, read from ``ENIKK_DRIVE_*`` environment variables."""

from __future__ import annotations

from pathlib import Path

from pydantic import Field

from enikk_core_client.tokens import PROJECTED_TOKEN_FILE
from enikk_kernel.settings import ServiceSettings


class DriveSettings(ServiceSettings, env_prefix="ENIKK_DRIVE_"):
    core_url: str = "http://enikk-core.enikk.svc"
    token_file: Path = PROJECTED_TOKEN_FILE
    oauth_token_url: str = "https://oauth2.googleapis.com/token"  # noqa: S105 - a URL
    api_url: str = "https://www.googleapis.com/drive/v3"
    upload_url: str = "https://www.googleapis.com/upload/drive/v3"
    root_folder: str = Field(default="enikk", min_length=1, max_length=255)
    """Name of the exchange folder at the top of My Drive. The tools reach nothing
    outside it (ADR-0053 §3). Created on first use when missing."""
    max_file_mb: int = Field(default=100, ge=1, le=2048)
    """Largest file one call may move, either way."""
    request_seconds: float = Field(default=300.0, gt=0)
    workspace: Path = Path("/data")
    """The runtime's shared volume (ADR-0037 §7): uploads are read from the calling
    thread's tmp or worktree, downloads go to ``tmp/<session>/drive``."""
    host: str = "0.0.0.0"  # noqa: S104 - reached only through its Service in the cluster.
    port: int = Field(default=8080, ge=1, le=65535)
    allowed_hosts: tuple[str, ...] = (
        "enikk-drive",
        "enikk-drive.enikk.svc",
        "enikk-drive.enikk.svc.cluster.local",
        # agentgateway passes on the Host its caller used (ADR-0039 S3).
        "enikk-agentgateway",
        "enikk-agentgateway.enikk.svc",
        "enikk-agentgateway.enikk.svc.cluster.local",
    )
    """Host header values the MCP endpoint accepts, with any port."""
    verbose: bool = False
