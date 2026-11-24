"""Settings, read from ``ENIKK_RUNTIME_*`` environment variables."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

from pydantic import Field

from enikk_core_client.tokens import PROJECTED_TOKEN_FILE
from enikk_kernel.settings import ServiceSettings, SettingsModel
from enikk_runtime.worker import WorkerPolicy
from enikk_runtime.workspace import WorkspaceConfig


class WorkerSettings(SettingsModel):
    wait_seconds: int = Field(default=20, ge=0, le=25)
    backoff_max_seconds: float = Field(default=30.0, gt=0)
    slots: int = Field(default=1, ge=1, le=4)
    """How many tasks run at the same time (ADR-0037, step 3a)."""

    def to_policy(self) -> WorkerPolicy:
        return WorkerPolicy(
            wait_seconds=self.wait_seconds,
            backoff_max_seconds=self.backoff_max_seconds,
            slots=self.slots,
        )


class WorkspaceSettings(SettingsModel):
    """The shared volume (ADR-0037 §7). Off unless the chart mounts one."""

    enabled: bool = False
    root: Path = Path("/data")
    repository: str = "404Vector/project-enikk"
    remote_url: str = "https://github.com/404Vector/project-enikk.git"
    branch: str = "main"
    fetch_interval_seconds: int = Field(default=60, ge=0)
    idle_days: int = Field(default=7, ge=1)
    min_free_mib: int = Field(default=512, ge=0)

    def to_config(self) -> WorkspaceConfig:
        return WorkspaceConfig(
            root=self.root,
            repository=self.repository,
            remote_url=self.remote_url,
            branch=self.branch,
            fetch_interval=timedelta(seconds=self.fetch_interval_seconds),
            idle_days=self.idle_days,
            min_free_bytes=self.min_free_mib * 1024 * 1024,
        )


class SkillSettings(SettingsModel):
    """Skills (ADR-0037 §2, §3): the skill list and ``load_skill`` for shared roles with
    built-in tools. Off unless the chart turns it on."""

    enabled: bool = False


class PullSettings(SettingsModel):
    """``submit_pull_request`` (ADR-0037 §6b): roles granted ``pull-requests`` with a
    thread worktree hand their commits to core. Needs the shared volume."""

    enabled: bool = False


class RuntimeClaudeSettings(ServiceSettings, env_prefix="ENIKK_RUNTIME_"):
    core_url: str = "http://enikk-core.enikk.svc"
    token_file: Path = PROJECTED_TOKEN_FILE
    langfuse_url: str = "https://jp.cloud.langfuse.com"
    workdir: Path = Path("/work")
    scratch_dir: Path = Path("/tmp/enikk")  # noqa: S108 - the Pod's private emptyDir
    tracing: bool = True
    verbose: bool = False
    worker: WorkerSettings = WorkerSettings()
    workspace: WorkspaceSettings = WorkspaceSettings()
    skills: SkillSettings = SkillSettings()
    pulls: PullSettings = PullSettings()
