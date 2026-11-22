"""Enikk's memory: notes from the owner's Obsidian vault (ADR-0028)."""

from __future__ import annotations

import uuid
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import Field, StringConstraints

from enikk_contracts.base import Contract

NotePath = Annotated[str, StringConstraints(min_length=1, max_length=500)]


class NoteKind(StrEnum):
    PREFERENCE = "preference"
    """The owner's preference. Long-term: kept until the owner changes it."""
    FACT = "fact"
    """A fact or reference. Long-term."""
    WORK = "work"
    """Work in progress. Short-term: it expires."""
    CONVERSATION = "conversation"
    """A day's summary of conversations."""
    NOTE = "note"
    """Any other note in the vault, mostly the owner's own."""


class MemoryNote(Contract):
    """A note handed to an agent with its task. Reference, never instructions."""

    path: NotePath
    title: Annotated[str, StringConstraints(max_length=300)]
    kind: NoteKind
    text: Annotated[str, StringConstraints(max_length=4_000)]


Title = Annotated[str, StringConstraints(min_length=1, max_length=80, strip_whitespace=True)]
MemoryText = Annotated[str, StringConstraints(min_length=1, max_length=1_000)]


class RememberRequest(Contract):
    """Keep a new note in enikk's memory. Only from a task the owner started."""

    lease_id: uuid.UUID
    kind: Literal[NoteKind.PREFERENCE, NoteKind.FACT, NoteKind.WORK]
    title: Title
    text: MemoryText


class UpdateMemoryRequest(Contract):
    lease_id: uuid.UUID
    path: NotePath
    text: MemoryText


class ForgetRequest(Contract):
    lease_id: uuid.UUID
    path: NotePath


class MemoryWritten(Contract):
    action: Literal["remembered", "updated", "forgotten"]
    path: NotePath


class MemorySearch(Contract):
    lease_id: uuid.UUID
    query: Annotated[str, StringConstraints(min_length=1, max_length=200)]
    limit: Annotated[int, Field(ge=1, le=20)] = 8


class MemoryMatch(Contract):
    path: NotePath
    title: Annotated[str, StringConstraints(max_length=300)]
    kind: NoteKind
    snippet: Annotated[str, StringConstraints(max_length=600)]


class MemoryMatches(Contract):
    matches: tuple[MemoryMatch, ...]
