"""Error hierarchy (P6). Interfaces translate these into messages and exit codes."""

from __future__ import annotations

import shlex
from collections.abc import Sequence


class EnikkError(Exception):
    """Root of every error raised by project code."""

    exit_code: int = 1


class ConfigurationError(EnikkError):
    """Invalid or missing configuration."""

    exit_code = 2


class ValidationError(EnikkError):
    """A value object or use-case input violated an invariant."""

    exit_code = 2


class ToolNotFoundError(EnikkError):
    """A required executable is not available on PATH."""

    exit_code = 127

    def __init__(self, tool: str) -> None:
        super().__init__(f"required executable not found on PATH: {tool}")
        self.tool = tool


class ExternalCommandError(EnikkError):
    """An external command exited with a non-zero status."""

    def __init__(self, args: Sequence[str], returncode: int, stderr: str = "") -> None:
        detail = f"\n{stderr.strip()}" if stderr.strip() else ""
        super().__init__(f"command failed ({returncode}): {shlex.join(args)}{detail}")
        self.args_ = tuple(args)
        self.returncode = returncode
        self.stderr = stderr
        self.exit_code = returncode or 1
