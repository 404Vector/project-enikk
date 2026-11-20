from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any

from enikk_platform.adapters.base import CliTool
from enikk_platform.domain import ReleaseInfo

if TYPE_CHECKING:
    from enikk_platform.domain import HelmRelease


class HelmCli(CliTool):
    """``ChartManager`` via the helm CLI.

    Override values are written to a temporary JSON values file (JSON is valid YAML)
    instead of ``--set`` so arbitrary strings (commas, quotes, newlines) are safe.
    """

    binary = "helm"

    def lint(self, chart: Path) -> str:
        return self._run("lint", str(chart), mutating=False).stdout

    def template(self, release: HelmRelease, values: Mapping[str, Any]) -> str:
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
        values: Mapping[str, Any],
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
            "status", release.name, *self._scope(release), "-o", "json",
            mutating=False, check=False,
        )
        if not result.ok:
            return None
        data = json.loads(result.stdout)
        return ReleaseInfo(
            name=data["name"],
            revision=int(data["version"]),
            status=data["info"]["status"],
        )

    # ------------------------------------------------------------------ helpers
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
    def _values_file(values: Mapping[str, Any]) -> Iterator[Path]:
        fd, name = tempfile.mkstemp(prefix="enikk-values-", suffix=".json")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(values, fh)
            yield Path(name)
        finally:
            Path(name).unlink(missing_ok=True)
