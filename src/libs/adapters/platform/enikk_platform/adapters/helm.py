from __future__ import annotations

import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict

from enikk_platform.adapters.base import CliTool
from enikk_platform.domain import ReleaseInfo

if TYPE_CHECKING:
    from collections.abc import Iterator

    from enikk_platform.domain import HelmRelease


class _HelmStatusInfo(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)
    status: str


class _HelmStatus(BaseModel):
    """Subset of ``helm status -o json`` output. Unknown fields are ignored."""

    model_config = ConfigDict(extra="ignore", frozen=True)
    name: str
    version: int
    info: _HelmStatusInfo


class HelmCli(CliTool):
    """``ChartManager`` via the helm CLI.

    Values are written to a temporary JSON values file (JSON is valid YAML), so
    arbitrary strings are passed safely without ``--set`` escaping.
    """

    binary = "helm"

    def lint(self, chart: Path) -> str:
        return self._run("lint", str(chart), mutating=False).stdout

    def template(self, release: HelmRelease, values: BaseModel) -> str:
        with self._values_file(values) as overrides:
            return self._run(
                "template",
                release.name,
                str(release.chart),
                "--namespace",
                release.target.namespace,
                *self._values_args(release, overrides),
                mutating=False,
            ).stdout

    def upgrade_install(
        self,
        release: HelmRelease,
        values: BaseModel,
        *,
        wait: bool = True,
        timeout: str = "5m",
    ) -> None:
        with self._values_file(values) as overrides:
            args = [
                "upgrade",
                "--install",
                release.name,
                str(release.chart),
                *self._scope(release),
                "--create-namespace",
                *self._values_args(release, overrides),
            ]
            if wait:
                args += ["--wait", "--timeout", timeout]
            self._run(*args, stream=True)

    def uninstall(self, release: HelmRelease) -> None:
        self._run("uninstall", release.name, *self._scope(release), stream=True)

    def status(self, release: HelmRelease) -> ReleaseInfo | None:
        result = self._run(
            "status",
            release.name,
            *self._scope(release),
            "--output",
            "json",
            mutating=False,
            check=False,
        )
        if not result.ok:
            return None
        parsed = _HelmStatus.model_validate_json(result.stdout)
        return ReleaseInfo(name=parsed.name, revision=parsed.version, status=parsed.info.status)

    @staticmethod
    def _scope(release: HelmRelease) -> list[str]:
        return ["--kube-context", release.target.context, "--namespace", release.target.namespace]

    @staticmethod
    def _values_args(release: HelmRelease, overrides: Path) -> list[str]:
        args: list[str] = []
        for path in (*release.values_files, overrides):
            args += ["--values", str(path)]
        return args

    @staticmethod
    @contextmanager
    def _values_file(values: BaseModel) -> Iterator[Path]:
        with tempfile.NamedTemporaryFile(
            "w", prefix="enikk-values-", suffix=".json", encoding="utf-8", delete=False
        ) as handle:
            handle.write(values.model_dump_json(by_alias=True, exclude_none=True))
            path = Path(handle.name)
        try:
            yield path
        finally:
            path.unlink(missing_ok=True)
