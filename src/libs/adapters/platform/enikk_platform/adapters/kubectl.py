from __future__ import annotations

import base64
from typing import TYPE_CHECKING, Literal

from pydantic import Field, SecretStr

from enikk_kernel.models import FrozenModel
from enikk_platform.adapters.base import CliTool

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from contextlib import AbstractContextManager
    from pathlib import Path

    from pydantic import BaseModel

    from enikk_platform.domain import KubeTarget


class _ObjectMeta(FrozenModel):
    name: str
    labels: dict[str, str] | None = None


class _Namespace(FrozenModel):
    api_version: Literal["v1"] = Field(default="v1", serialization_alias="apiVersion")
    kind: Literal["Namespace"] = "Namespace"
    metadata: _ObjectMeta


class _Secret(FrozenModel):
    api_version: Literal["v1"] = Field(default="v1", serialization_alias="apiVersion")
    kind: Literal["Secret"] = "Secret"
    metadata: _ObjectMeta
    type: Literal["Opaque"] = "Opaque"
    data: dict[str, str]


class KubectlCli(CliTool):
    """``ClusterClient`` via kubectl. Manifests are sent on stdin, never argv."""

    binary = "kubectl"

    def ensure_namespace(self, target: KubeTarget) -> None:
        manifest = _Namespace(metadata=_ObjectMeta(name=target.namespace))
        self._run("--context", target.context, "apply", "-f", "-", stdin=self._serialise(manifest))

    def apply(self, target: KubeTarget, manifest: BaseModel) -> None:
        self._run(*self._scope(target), "apply", "-f", "-", stdin=self._serialise(manifest))

    def apply_file(self, target: KubeTarget, path: Path) -> None:
        self._run(*self._scope(target), "apply", "-f", str(path))

    def apply_secret(
        self, target: KubeTarget, name: str, data: Mapping[str, SecretStr], *, manager: str
    ) -> None:
        manifest = _Secret(
            metadata=_ObjectMeta(name=name, labels={"app.kubernetes.io/managed-by": manager}),
            data={
                key: base64.b64encode(value.get_secret_value().encode()).decode()
                for key, value in sorted(data.items())
            },
        )
        self._run(
            *self._scope(target),
            "apply",
            "--server-side",
            "--force-conflicts",
            f"--field-manager={manager}",
            "-f",
            "-",
            stdin=self._serialise(manifest),
        )

    def port_forward(
        self, target: KubeTarget, resource: str, local_port: int, remote_port: int
    ) -> int:
        result = self._run(
            *self._scope(target),
            "port-forward",
            resource,
            f"{local_port}:{remote_port}",
            mutating=False,
            stream=True,
            check=False,
        )
        return result.returncode

    def forwarding(
        self, target: KubeTarget, resource: str, local_port: int, remote_port: int
    ) -> AbstractContextManager[None]:
        return self._runner.background(
            [
                self._executable,
                *self._scope(target),
                "port-forward",
                resource,
                f"{local_port}:{remote_port}",
            ]
        )

    def create_token(
        self, target: KubeTarget, service_account: str, *, audience: str, duration: str = "10m"
    ) -> SecretStr:
        result = self._run(
            *self._scope(target),
            "create",
            "token",
            service_account,
            "--audience",
            audience,
            "--duration",
            duration,
            mutating=False,
        )
        return SecretStr(result.stdout.strip())

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
            *self._scope(target),
            "exec",
            *flags,
            resource,
            "--",
            *command,
            stream=True,
            check=False,
        )
        return result.returncode

    def logs(self, target: KubeTarget, resource: str, *, follow: bool = True) -> int:
        args = [*self._scope(target), "logs", resource, "--pod-running-timeout=5m"]
        if follow:
            args.append("--follow")
        return self._run(*args, mutating=False, stream=True, check=False).returncode

    @staticmethod
    def _serialise(manifest: BaseModel) -> str:
        return manifest.model_dump_json(by_alias=True, exclude_none=True)

    @staticmethod
    def _scope(target: KubeTarget) -> list[str]:
        return ["--context", target.context, "--namespace", target.namespace]
