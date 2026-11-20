"""Ports for build and deployment tooling."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from pydantic import BaseModel

    from enikk_platform.domain import HelmRelease, ImageRef, KubeTarget, ReleaseInfo


class ImageBuilder(Protocol):
    def build(self, image: ImageRef, context: Path) -> None: ...


class ChartManager(Protocol):
    """Helm operations. ``values`` is a pydantic model serialised by alias."""

    def lint(self, chart: Path) -> str: ...

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
    """kubectl operations. Manifests are pydantic models serialised by alias."""

    def ensure_namespace(self, target: KubeTarget) -> None: ...

    def apply(self, target: KubeTarget, manifest: BaseModel) -> None: ...

    def exists(self, target: KubeTarget, kind: str, name: str) -> bool: ...

    def describe(
        self, target: KubeTarget, kinds: Sequence[str], selector: str | None = None
    ) -> str: ...

    def exec(
        self, target: KubeTarget, resource: str, command: Sequence[str], *, tty: bool = True
    ) -> int: ...

    def logs(self, target: KubeTarget, resource: str, *, follow: bool = True) -> int: ...
