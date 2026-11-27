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


NoteText = Annotated[str, StringConstraints(min_length=1, max_length=100_000)]


class NoteRead(Contract):
    """Read one note of the vault as it is, frontmatter included (ADR-0049)."""

    lease_id: uuid.UUID
    path: NotePath


class NoteContent(Contract):
    path: NotePath
    exists: bool
    text: Annotated[str, StringConstraints(max_length=100_000)] = ""


class NoteWrite(Contract):
    """Create or replace a public note of the vault, the owner's blog (ADR-0049).

    ``text`` is the whole file, frontmatter included. Only from a task the owner
    started; a new note must be a draft, and a write never changes whether a note
    is published."""

    lease_id: uuid.UUID
    path: NotePath
    text: NoteText


class NoteWritten(Contract):
    action: Literal["created", "updated"]
    path: NotePath
    draft: bool


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
