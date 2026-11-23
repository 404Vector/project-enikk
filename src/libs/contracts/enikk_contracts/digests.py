"""Daily conversation summaries in the owner's vault (ADR-0035).

Runtime → core: the owner asks for one day's summary by hand. Core → connector: a
summary could not be made.
"""

from __future__ import annotations

import uuid
from datetime import date
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import StringConstraints

from enikk_contracts.base import Contract

Detail = Annotated[str, StringConstraints(max_length=500)]


class DigestStatus(StrEnum):
    RUNNING = "running"
    """The summarizer role has the day's conversations and is writing the summary."""
    WRITTEN = "written"
    """The summary is in the vault."""
    EMPTY = "empty"
    """There were no conversations that day; no file was made."""
    FAILED = "failed"


class DigestRequest(Contract):
    """Make (or remake) the summary of ``day``, a date in the owner's time zone."""

    lease_id: uuid.UUID
    day: date


class DigestAccepted(Contract):
    day: date
    status: DigestStatus
    detail: Detail = ""


class DigestFailed(Contract):
    """One line in the owner's DM: the summary of ``day`` could not be made."""

    kind: Literal["digest-failed"] = "digest-failed"
    day: date
    reason: Detail
    retry: bool
    """Whether core will try once more on the next day's run."""
