from __future__ import annotations

from typing import TYPE_CHECKING

from enikk_platform.adapters.base import CliTool

if TYPE_CHECKING:
    from pathlib import Path

    from enikk_platform.domain import ImageRef


class DockerCli(CliTool):
    """``ImageBuilder`` via the docker CLI."""

    binary = "docker"

    def build(self, image: ImageRef, context: Path) -> None:
        self._run("build", "-t", str(image), str(context), stream=True)
