from __future__ import annotations

from typing import TYPE_CHECKING

from enikk_platform.adapters.base import CliTool

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

    from enikk_platform.domain import ImageRef


class DockerCli(CliTool):
    """``ImageBuilder`` via the docker CLI. Docker Desktop k8s shares the local image store."""

    binary = "docker"

    def build(
        self, image: ImageRef, context: Path, *, build_args: Mapping[str, str] | None = None
    ) -> None:
        args = ["build", "-t", str(image)]
        for key, value in (build_args or {}).items():
            args += ["--build-arg", f"{key}={value}"]
        args.append(str(context))
        self._run(*args, stream=True)
