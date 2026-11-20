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


class OpsSettings(ServiceSettings, env_prefix="ENIKK_OPS_"):
    kube_context: str = "docker-desktop"
    repo_root: Path = Path()
    argocd: ArgoCdSettings = ArgoCdSettings()
