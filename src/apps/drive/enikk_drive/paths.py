"""Paths inside the exchange folder, as the tools take them: ``to-unity/hero.png``."""

from __future__ import annotations

import unicodedata
from typing import Final

from enikk_kernel.errors import EnikkError

MAX_DEPTH: Final = 8
MAX_SEGMENT: Final = 255


class DrivePathError(EnikkError):
    """A path the tools do not accept."""


def parse(text: str, *, allow_root: bool = False) -> tuple[str, ...]:
    """The path's folder and file names, top first. Leading and trailing slashes are
    ignored; ``.``, ``..``, empty names, backslashes and control characters are not."""
    stripped = text.strip().strip("/")
    if not stripped:
        if allow_root:
            return ()
        raise DrivePathError("give a path inside the exchange folder, e.g. to-unity/hero.png")
    segments = tuple(stripped.split("/"))
    if len(segments) > MAX_DEPTH:
        raise DrivePathError(f"{text}: at most {MAX_DEPTH} levels")
    for segment in segments:
        _check(text, segment)
    return segments


def join(segments: tuple[str, ...]) -> str:
    return "/".join(segments)


def _check(text: str, segment: str) -> None:
    if segment in {"", ".", ".."}:
        raise DrivePathError(f"{text}: empty, '.' and '..' names are not allowed")
    if segment != segment.strip():
        raise DrivePathError(f"{text}: names may not start or end with spaces")
    if len(segment) > MAX_SEGMENT:
        raise DrivePathError(f"{text}: a name is longer than {MAX_SEGMENT} characters")
    if "\\" in segment or any(unicodedata.category(c) == "Cc" for c in segment):
        raise DrivePathError(f"{text}: backslashes and control characters are not allowed")
