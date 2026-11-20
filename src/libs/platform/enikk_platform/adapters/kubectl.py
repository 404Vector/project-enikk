from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from enikk_platform.adapters.base import CliTool

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from enikk_platform.domain import KubeTarget


class KubectlCli(CliTool):
    """``ClusterClient`` via kubectl. Manifests are sent on stdin (never argv)."""

    binary = "kubectl"

    def ensure_namespace(self, target: KubeTarget) -> None:
        manifest = {"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": target.namespace}}
        self._run("--context", target.context, "apply", "-f", "-", input=json.dumps(manifest))

    def apply(self, target: KubeTarget, manifest: Mapping[str, Any]) -> None:
        self._run(*self._scope(target), "apply", "-f", "-", input=json.dumps(manifest))

    def exists(self, target: KubeTarget, kind: str, name: str) -> bool:
        result = self._run(
            *self._scope(target), "get", kind, name, "-o", "name", mutating=False, check=False
        )
        return result.ok

    def describe(
        self, target: KubeTarget, kinds: Sequence[str], selector: str | None = None
    ) -> str:
        args = [*self._scope(target), "get", ",".join(kinds)]
        if selector:
            args += ["-l", selector]
        return self._run(*args, mutating=False).stdout

    def exec(
        self, target: KubeTarget, resource: str, command: Sequence[str], *, tty: bool = True
    ) -> int:
        flags = ["-it"] if tty else ["-i"]
        result = self._run(
            *self._scope(target), "exec", *flags, resource, "--", *command,
            stream=True, check=False,
        )
        return result.returncode

    def logs(self, target: KubeTarget, resource: str, *, follow: bool = True) -> int:
        args = [*self._scope(target), "logs", resource, "--pod-running-timeout=5m"]
        if follow:
            args.append("--follow")
        return self._run(*args, mutating=False, stream=True, check=False).returncode

    @staticmethod
    def _scope(target: KubeTarget) -> list[str]:
        return ["--context", target.context, "--namespace", target.namespace]
