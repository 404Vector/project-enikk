"""Ops CLI settings, read from ``ENIKK_OPS_*`` environment variables."""

from __future__ import annotations

from pathlib import Path

from pydantic import Field

from enikk_kernel.settings import ServiceSettings, SettingsModel


class ArgoCdSettings(SettingsModel):
    namespace: str = "argocd"
    release: str = "argocd"
    chart_repository: str = "https://argoproj.github.io/argo-helm"
    chart: str = "argo-cd"
    chart_version: str = "10.18.2"
    values_file: Path = Path("deploy/argocd/bootstrap/argocd-values.yaml")
    root_application: Path = Path("deploy/argocd/root.yaml")
    server_service: str = "svc/argocd-server"
    ui_port: int = Field(default=8080, ge=1024, le=65535)


class SmokeSettings(SettingsModel):
    namespace: str = "enikk"
    service: str = "svc/enikk-core"
    service_port: int = Field(default=80, ge=1, le=65535)
    local_port: int = Field(default=18000, ge=1024, le=65535)
    connector_account: str = "enikk-smoke"
    runtime_account: str = "enikk-runtime-claude"
    unbound_account: str = "default"
    reply_timeout_seconds: int = Field(default=300, ge=10, le=1800)
    dev_timeout_seconds: float = Field(default=2400, gt=0)


def _default_secret_file() -> Path:
    return Path.home() / ".config" / "enikk" / "secrets.yaml"


class SecretsSettings(SettingsModel):
    file: Path = Field(default_factory=_default_secret_file)
    namespace: str = "enikk"
    secret_name: str = "enikk-core-credentials"  # noqa: S105 - a resource name, not a value


class OpsSettings(ServiceSettings, env_prefix="ENIKK_OPS_"):
    kube_context: str = "docker-desktop"
    repo_root: Path = Path()
    argocd: ArgoCdSettings = ArgoCdSettings()
    smoke: SmokeSettings = SmokeSettings()
    # Built per instance so the home directory is resolved when settings load.
    secrets: SecretsSettings = Field(default_factory=SecretsSettings)
