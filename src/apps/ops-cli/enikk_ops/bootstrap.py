"""Cluster bootstrap use cases."""

from __future__ import annotations

from typing import TYPE_CHECKING

from enikk_kernel.errors import ConfigurationError
from enikk_kernel.models import FrozenModel
from enikk_platform.domain import HelmRelease, KubeTarget, RemoteChart

if TYPE_CHECKING:
    from pathlib import Path

    from enikk_ops.settings import OpsSettings
    from enikk_platform.ports import ChartManager, ClusterClient


class _NoOverrides(FrozenModel):
    """Empty values override; chart values come from the values file in git."""


class ArgoCdService:
    """Installs Argo CD and hands the cluster over to the root Application in git."""

    def __init__(self, settings: OpsSettings, charts: ChartManager, cluster: ClusterClient) -> None:
        self._settings = settings
        self._charts = charts
        self._cluster = cluster
        self._target = KubeTarget(
            context=settings.kube_context, namespace=settings.argocd.namespace
        )

    def bootstrap(self) -> None:
        argocd = self._settings.argocd
        release = HelmRelease(
            name=argocd.release,
            chart=RemoteChart(
                repository=argocd.chart_repository,
                name=argocd.chart,
                version=argocd.chart_version,
            ),
            target=self._target,
            values_files=(self._existing(argocd.values_file),),
        )
        self._charts.upgrade_install(release, _NoOverrides(), wait=True, timeout="10m")
        self._cluster.apply_file(self._target, self._existing(argocd.root_application))

    def status(self) -> str:
        return self._cluster.describe(self._target, ["applications.argoproj.io"])

    def open_ui(self) -> int:
        port = self._settings.argocd.ui_port
        return self._cluster.port_forward(
            self._target, self._settings.argocd.server_service, port, 80
        )

    def _existing(self, relative: Path) -> Path:
        path = (self._settings.repo_root / relative).resolve()
        if not path.is_file():
            raise ConfigurationError(f"file not found: {path} (run from the repository root)")
        return path
