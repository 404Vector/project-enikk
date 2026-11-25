"""Settings, read from ``ENIKK_CONNECTOR_*`` environment variables."""

from __future__ import annotations

from pathlib import Path

from pydantic import Field

from enikk_contracts.outbox import MAX_ACK_BATCH
from enikk_contracts.tasks import MAX_WAIT_SECONDS
from enikk_core_client.tokens import PROJECTED_TOKEN_FILE
from enikk_kernel.settings import ServiceSettings, SettingsModel


class OutboxSettings(SettingsModel):
    wait_seconds: int = Field(default=20, ge=0, le=MAX_WAIT_SECONDS)
    batch: int = Field(default=20, ge=1, le=MAX_ACK_BATCH)
    backoff_max_seconds: float = Field(default=30.0, gt=0)


class ConnectorSlackSettings(ServiceSettings, env_prefix="ENIKK_CONNECTOR_"):
    core_url: str = "http://enikk-core.enikk.svc"
    token_file: Path = PROJECTED_TOKEN_FILE
    config_file: Path = Path("/app/config/slack.yaml")
    # Slack expects a Socket Mode acknowledgement within 3 seconds.
    submit_timeout_seconds: float = Field(default=2.5, gt=0, lt=3)
    verbose: bool = False
    outbox: OutboxSettings = OutboxSettings()
    deploy_notify_channel: str | None = None
    """Overrides ``deploy_notify_channel`` of the Slack config (ADR-0026)."""
    approval_channel: str | None = None
    """Overrides ``approval_channel`` of the Slack config (ADR-0036)."""
    workspace_report_seconds: float = Field(default=600.0, ge=30)
    """How often core hears the workspace address and custom emoji (ADR-0042)."""
