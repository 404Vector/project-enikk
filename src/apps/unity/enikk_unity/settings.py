"""Settings, read from ``ENIKK_UNITY_*`` environment variables."""

from __future__ import annotations

from pathlib import Path

from pydantic import Field

from enikk_core_client.tokens import PROJECTED_TOKEN_FILE
from enikk_kernel.settings import ServiceSettings


class UnitySettings(ServiceSettings, env_prefix="ENIKK_UNITY_"):
    core_url: str = "http://enikk-core.enikk.svc"
    token_file: Path = PROJECTED_TOKEN_FILE
    upstream_url: str = "http://192.168.137.1:8765/mcp"
    """The Unity MCP server on the owner's Windows PC, over its hotspot (ADR-0044)."""
    instance: str = ""
    """Unity editor instance selected at the start of each Unity session; empty selects
    none."""
    protocol_version: str = "2025-06-18"
    """MCP revision spoken to the Unity server."""
    connect_seconds: float = Field(default=5.0, gt=0)
    request_seconds: float = Field(default=300.0, gt=0)
    """One call to the Unity server, tests and builds included."""
    host: str = "0.0.0.0"  # noqa: S104 - reached only through its Service in the cluster.
    port: int = Field(default=8080, ge=1, le=65535)
    allowed_hosts: tuple[str, ...] = (
        "enikk-unity",
        "enikk-unity.enikk.svc",
        "enikk-unity.enikk.svc.cluster.local",
        # agentgateway passes on the Host its caller used (ADR-0039 S3).
        "enikk-agentgateway",
        "enikk-agentgateway.enikk.svc",
        "enikk-agentgateway.enikk.svc.cluster.local",
    )
    """Host header values the MCP endpoint accepts, with any port."""
    verbose: bool = False
