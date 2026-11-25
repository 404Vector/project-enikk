"""enikk-core settings, read from ``ENIKK_CORE_*`` environment variables."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import Literal, Self

from pydantic import Field, SecretStr, model_validator

from enikk_contracts.common import ConnectorKind, Engine
from enikk_contracts.credentials import CORE_ONLY, CredentialName
from enikk_core_domain.auth import (
    ConnectorRole,
    Role,
    RoleBinding,
    RuntimeRole,
    SchedulerRole,
    ServiceIdentity,
)
from enikk_github.app import GitHubAppConfig
from enikk_kernel.errors import ConfigurationError
from enikk_kernel.settings import ServiceSettings, SettingsModel
from enikk_persistence_sql.engine import DatabaseConfig


class DatabaseSettings(SettingsModel):
    """Filled from the CloudNativePG application Secret (``enikk-db-app``)."""

    host: str = "localhost"
    port: int = Field(default=5432, ge=1, le=65535)
    name: str = "enikk"
    user: str = "enikk"
    password: SecretStr = SecretStr("")
    pool_size: int = Field(default=5, ge=1, le=50)

    def to_config(self) -> DatabaseConfig:
        return DatabaseConfig(
            host=self.host,
            port=self.port,
            name=self.name,
            user=self.user,
            password=self.password,
            pool_size=self.pool_size,
        )


class RoleBindingSettings(SettingsModel):
    namespace: str
    service_account: str
    role: Literal["connector", "runtime", "scheduler"]
    connector: ConnectorKind | None = None
    engine: Engine | None = None
    credentials: tuple[CredentialName, ...] = ()

    @model_validator(mode="after")
    def _role_fields(self) -> Self:
        if (self.role == "connector") != (self.connector is not None):
            raise ValueError("'connector' is required exactly for the connector role")
        if (self.role == "runtime") != (self.engine is not None):
            raise ValueError("'engine' is required exactly for the runtime role")
        core_only = sorted(set(self.credentials) & CORE_ONLY)
        if core_only:
            raise ValueError(f"{core_only} never leave core (I-32)")
        return self

    def to_binding(self) -> RoleBinding:
        role: Role
        if self.connector is not None:
            role = ConnectorRole(connector=self.connector)
        elif self.engine is not None:
            role = RuntimeRole(engine=self.engine)
        elif self.role == "scheduler":
            role = SchedulerRole()
        else:
            raise ConfigurationError(f"role binding {self.service_account} has no role")
        identity = ServiceIdentity(namespace=self.namespace, name=self.service_account)
        return RoleBinding(identity=identity, role=role, credentials=frozenset(self.credentials))


class TaskSettings(SettingsModel):
    lease_seconds: int = Field(default=120, ge=10, le=3600)
    max_attempts: int = Field(default=3, ge=1, le=10)
    poll_interval_seconds: float = Field(default=1.0, gt=0, le=5)
    reaper_interval_seconds: float = Field(default=10.0, gt=0, le=300)
    reaper_batch: int = Field(default=50, ge=1, le=1000)

    @property
    def lease_duration(self) -> timedelta:
        return timedelta(seconds=self.lease_seconds)


class TokenReviewSettings(SettingsModel):
    api_url: str = "https://kubernetes.default.svc"
    cache_seconds: int = Field(default=90, ge=1, le=300)


class GitHubSettings(SettingsModel):
    """The GitHub App (ADR-0019). Without both IDs, core does not use GitHub."""

    app_id: int | None = Field(default=None, gt=0)
    installation_id: int | None = Field(default=None, gt=0)
    owner: str = "404Vector"
    api_url: str = "https://api.github.com"
    self_repository: str = "project-enikk"
    repositories_file: Path = Path("config/github.yaml")
    sensitive_paths_file: Path = Path("config/sensitive-paths.yaml")

    def to_config(self) -> GitHubAppConfig | None:
        if self.app_id is None or self.installation_id is None:
            return None
        return GitHubAppConfig(
            app_id=self.app_id,
            installation_id=self.installation_id,
            owner=self.owner,
            api_url=self.api_url,
        )


class ApprovalSettings(SettingsModel):
    """Slack approval of pull requests (ADR-0020). Needs the GitHub App."""

    enabled: bool = False
    interval_seconds: float = Field(default=30.0, ge=5, le=600)


class DeploymentSettings(SettingsModel):
    """Deployment and build notices (ADR-0023). Needs the GitHub App for PR links."""

    enabled: bool = False
    interval_seconds: float = Field(default=30.0, ge=5, le=600)
    argocd_namespace: str = "argocd"
    apps: tuple[str, ...] = ()
    """Argo CD Applications that deploy the platform's own repository."""
    images_workflow: str = "images.yml"
    timeout_seconds: float = Field(default=600.0, ge=60, le=7200)
    settle_seconds: float = Field(default=60.0, ge=0, le=600)
    api_url: str = "https://kubernetes.default.svc"


class MemorySettings(SettingsModel):
    """Enikk's memory in the owner's vault (ADR-0028). Needs the GitHub App."""

    enabled: bool = False
    repository: str = "vault.hyeongseok-kim"
    branch: str = "main"
    notes_root: str = "vault/"
    enikk_dir: str = "vault/Private/enikk/"
    interval_seconds: float = Field(default=60.0, ge=15, le=3600)
    context_chars: int = Field(default=4_000, ge=0, le=20_000)


class DigestSettings(SettingsModel):
    """Daily conversation summaries in the vault (ADR-0035). Needs enikk's memory."""

    enabled: bool = False
    interval_seconds: float = Field(default=300.0, ge=15, le=3600)
    timezone: str = "Asia/Seoul"
    run_hour: int = Field(default=4, ge=0, le=23)
    catchup_days: int = Field(default=2, ge=1, le=14)
    max_auto_attempts: int = Field(default=2, ge=1, le=5)
    agent: str = "summarizer"


class DiagnosticsSettings(SettingsModel):
    """Read-only diagnostics for enikk (ADR-0029)."""

    enabled: bool = False
    namespace: str = "enikk"
    api_url: str = "https://kubernetes.default.svc"


class PromptSettings(SettingsModel):
    """Prompt candidates and their promotion in Slack (ADR-0032). Uses core's Langfuse keys."""

    enabled: bool = False
    langfuse_url: str = Field(
        default="https://jp.cloud.langfuse.com", pattern=r"^https?://[^/\s]+$"
    )
    slack_config_file: Path = Path("config/slack.yaml")
    """The owner's Slack user ID is read from here; only they decide (I-01)."""


class SubmissionSettings(SettingsModel):
    """Core pushes a Slack thread's commits and opens the PR (ADR-0037 §6b). Needs the
    GitHub App and git in core's image."""

    enabled: bool = False
    clone_dir: Path = Path("/var/lib/enikk/clone")
    """Core's own clone, on an emptyDir. Never the runtime's shared volume."""
    co_author: str = "Claude <noreply@anthropic.com>"
    git_timeout_seconds: int = Field(default=120, ge=1)


class WakeupSettings(SettingsModel):
    """Wake a thread's session when its PR or prompt candidate moves on (ADR-0041 §4)."""

    enabled: bool = False


class CoreSettings(ServiceSettings, env_prefix="ENIKK_CORE_"):
    host: str = "0.0.0.0"  # noqa: S104 - must accept traffic from the pod network
    port: int = Field(default=8000, ge=1, le=65535)
    version: str = "dev"
    verbose: bool = False
    agents_dir: Path = Path("agents")
    credentials_dir: Path = Path("/var/run/secrets/enikk/credentials")
    database: DatabaseSettings = DatabaseSettings()
    tasks: TaskSettings = TaskSettings()
    token_review: TokenReviewSettings = TokenReviewSettings()
    github: GitHubSettings = GitHubSettings()
    approval: ApprovalSettings = ApprovalSettings()
    deployments: DeploymentSettings = DeploymentSettings()
    memory: MemorySettings = MemorySettings()
    digests: DigestSettings = DigestSettings()
    diagnostics: DiagnosticsSettings = DiagnosticsSettings()
    prompts: PromptSettings = PromptSettings()
    submissions: SubmissionSettings = SubmissionSettings()
    wakeups: WakeupSettings = WakeupSettings()
    # Identities allowed to call core (ADR-0011 §5). Empty: every caller is denied.
    role_bindings: tuple[RoleBindingSettings, ...] = ()
