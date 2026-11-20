"""Ports (P2): the only contracts the application layer knows about."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from pathlib import Path

    from enikk_platform.domain import HelmRelease, ImageRef, KubeTarget, ReleaseInfo


class ImageBuilder(Protocol):
    def build(
        self, image: ImageRef, context: Path, *, build_args: Mapping[str, str] | None = None
    ) -> None: ...


class ChartManager(Protocol):
    def lint(self, chart: Path) -> str: ...

    def template(self, release: HelmRelease, values: Mapping[str, Any]) -> str: ...

    def upgrade_install(
        self,
        release: HelmRelease,
        values: Mapping[str, Any],
        *,
        wait: bool = True,
        timeout: str = "5m",
    ) -> None: ...

    def uninstall(self, release: HelmRelease) -> None: ...

    def status(self, release: HelmRelease) -> ReleaseInfo | None: ...


class ClusterClient(Protocol):
    def ensure_namespace(self, target: KubeTarget) -> None: ...

    def apply(self, target: KubeTarget, manifest: Mapping[str, Any]) -> None: ...

    def exists(self, target: KubeTarget, kind: str, name: str) -> bool: ...

    def describe(self, target: KubeTarget, kinds: Sequence[str], selector: str | None = None) -> str: ...

    def exec(
        self, target: KubeTarget, resource: str, command: Sequence[str], *, tty: bool = True
    ) -> int: ...

    def logs(self, target: KubeTarget, resource: str, *, follow: bool = True) -> int: ...
