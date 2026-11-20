"""Project root discovery."""

from __future__ import annotations

from pathlib import Path

from enikk_core.errors import ConfigurationError


def find_project_root(start: Path | None = None, *, marker: str = "enikk.toml") -> Path:
    """Walk up from ``start`` (default: cwd) until a directory containing ``marker`` is found."""
    current = (start or Path.cwd()).resolve()
    for candidate in (current, *current.parents):
        if (candidate / marker).is_file():
            return candidate
    raise ConfigurationError(f"could not find {marker} in {current} or any parent directory")
