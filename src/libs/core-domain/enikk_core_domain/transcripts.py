"""Rules shared by every ``TranscriptRepository`` implementation (ADR-0014)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

    from enikk_contracts.transcripts import TranscriptEntry

MAIN_SUBPATH: Final = ""
"""Stored subpath of a main (non-subagent) transcript."""

MAX_UUID_LENGTH: Final = 255


def entry_uuid(entry: TranscriptEntry) -> str | None:
    """The entry's ``uuid`` field, used to skip duplicates. ``None`` if absent or unusable."""
    value = entry.get("uuid")
    if isinstance(value, str) and 0 < len(value) <= MAX_UUID_LENGTH:
        return value
    return None


def new_entries(
    entries: Sequence[TranscriptEntry], stored_uuids: Iterable[str]
) -> list[TranscriptEntry]:
    """Entries to store, in order: drop those whose ``uuid`` is stored or repeated."""
    seen = set(stored_uuids)
    kept: list[TranscriptEntry] = []
    for entry in entries:
        marker = entry_uuid(entry)
        if marker is not None:
            if marker in seen:
                continue
            seen.add(marker)
        kept.append(entry)
    return kept
