"""Settings, read from ``ENIKK_RUNTIME_*`` environment variables."""

from __future__ import annotations

from pathlib import Path

from pydantic import Field

from enikk_core_client.tokens import PROJECTED_TOKEN_FILE
from enikk_kernel.settings import ServiceSettings, SettingsModel
from enikk_runtime.worker import WorkerPolicy


class WorkerSettings(SettingsModel):
    wait_seconds: int = Field(default=20, ge=0, le=25)
    backoff_max_seconds: float = Field(default=30.0, gt=0)

    def to_policy(self) -> WorkerPolicy:
        return WorkerPolicy(
            wait_seconds=self.wait_seconds, backoff_max_seconds=self.backoff_max_seconds
        )


class RuntimeClaudeSettings(ServiceSettings, env_prefix="ENIKK_RUNTIME_"):
    core_url: str = "http://enikk-core.enikk.svc"
    token_file: Path = PROJECTED_TOKEN_FILE
    langfuse_url: str = "https://jp.cloud.langfuse.com"
    workdir: Path = Path("/work")
    scratch_dir: Path = Path("/tmp/enikk")  # noqa: S108 - the Pod's private emptyDir
    tracing: bool = True
    verbose: bool = False
    worker: WorkerSettings = WorkerSettings()
