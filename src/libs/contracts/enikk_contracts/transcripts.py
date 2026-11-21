"""Runtime ↔ core: engine conversation transcripts (ADR-0014)."""

from __future__ import annotations

import uuid
from typing import Annotated, Final

from pydantic import Field, JsonValue, StringConstraints

from enikk_contracts.base import Contract
from enikk_contracts.common import Identifier

MAX_ENTRIES_PER_APPEND: Final = 1_000

TranscriptEntry = dict[str, JsonValue]
"""One transcript line in the engine's own format. Core stores it without interpreting it."""

Subpath = Annotated[str, StringConstraints(min_length=1, max_length=255)]


class TranscriptKey(Contract):
    """Identifies one transcript inside the session that owns the leased task."""

    project_key: Identifier
    engine_session_id: Identifier
    subpath: Subpath | None = None


class TranscriptAppend(Contract):
    """Append entries in order. Entries whose ``uuid`` was stored before are skipped."""

    lease_id: uuid.UUID
    key: TranscriptKey
    entries: Annotated[
        tuple[TranscriptEntry, ...], Field(min_length=1, max_length=MAX_ENTRIES_PER_APPEND)
    ]


class TranscriptAppended(Contract):
    stored: Annotated[int, Field(ge=0)]


class TranscriptQuery(Contract):
    lease_id: uuid.UUID
    key: TranscriptKey


class TranscriptLoaded(Contract):
    """Entries in append order. Empty when the transcript does not exist."""

    entries: tuple[TranscriptEntry, ...]


class SubpathQuery(Contract):
    lease_id: uuid.UUID
    project_key: Identifier
    engine_session_id: Identifier


class TranscriptSubpaths(Contract):
    subpaths: tuple[Subpath, ...]
