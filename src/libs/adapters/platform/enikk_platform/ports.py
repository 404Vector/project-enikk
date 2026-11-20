"""Ports for build and deployment tooling."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from collections.abc import Sequence
    from contextlib import AbstractContextManager
    from pathlib import Path

    from pydantic import BaseModel, SecretStr

    from enikk_platform.domain import ChartRef, HelmRelease, ImageRef, KubeTarget, ReleaseInfo


class ImageBuilder(Protocol):
    def build(self, image: ImageRef, context: Path) -> None: ...


class ChartManager(Protocol):
    """Helm operations. ``values`` is a pydantic model serialised by alias."""

    def lint(self, chart: ChartRef) -> str: ...

    def template(self, release: HelmRelease, values: BaseModel) -> str: ...

    def upgrade_install(
        self,
        release: HelmRelease,
        values: BaseModel,
        *,
        wait: bool = True,
        timeout: str = "5m",
    ) -> None: ...

    def uninstall(self, release: HelmRelease) -> None: ...

    def status(self, release: HelmRelease) -> ReleaseInfo | None: ...


class ClusterClient(Protocol):
    """kubectl operations. In-memory manifests are pydantic models serialised by alias."""

    def ensure_namespace(self, target: KubeTarget) -> None: ...

    def apply(self, target: KubeTarget, manifest: BaseModel) -> None: ...

    def apply_file(self, target: KubeTarget, path: Path) -> None: ...

    def exists(self, target: KubeTarget, kind: str, name: str) -> bool: ...

    def describe(
        self, target: KubeTarget, kinds: Sequence[str], selector: str | None = None
    ) -> str: ...

    def exec(
        self, target: KubeTarget, resource: str, command: Sequence[str], *, tty: bool = True
    ) -> int: ...

    def logs(self, target: KubeTarget, resource: str, *, follow: bool = True) -> int: ...

    def port_forward(
        self, target: KubeTarget, resource: str, local_port: int, remote_port: int
    ) -> int: ...

    def forwarding(
        self, target: KubeTarget, resource: str, local_port: int, remote_port: int
    ) -> AbstractContextManager[None]:
        """Keep a port-forward open while the ``with`` block runs."""
        ...

    def create_token(
        self, target: KubeTarget, service_account: str, *, audience: str, duration: str = "10m"
    ) -> SecretStr:
        """Issue a short-lived ServiceAccount token (TokenRequest). Changes no resources."""
        ...
