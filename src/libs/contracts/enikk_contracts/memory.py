"""Enikk's memory: notes from the owner's Obsidian vault (ADR-0028)."""

from __future__ import annotations

import uuid
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import Field, StringConstraints

from enikk_contracts.base import Contract
from enikk_contracts.common import ConversationRef, Identifier

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
    """True when the blog does not publish the note: a draft or an unpublished folder."""


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


# ------------------------------------------------ changes that need the owner (ADR-0051 §4)


class NoteChangeAction(StrEnum):
    PUBLISH = "publish"
    """Remove ``draft: true``: the blog publishes the note."""
    UNPUBLISH = "unpublish"
    """Add ``draft: true``: the blog takes the note down."""
    DELETE = "delete"
    MOVE = "move"
    """Rename or move the note to ``to``."""


class NoteChangeStatus(StrEnum):
    REQUESTED = "requested"
    DONE = "done"
    REJECTED = "rejected"
    STALE = "stale"
    """The note changed after the request; nothing was done."""


Reason = Annotated[str, StringConstraints(min_length=1, max_length=500)]


class NoteChangeRequest(Contract):
    """Ask the owner to publish, unpublish, delete or move a note. Changes nothing yet."""

    lease_id: uuid.UUID
    action: NoteChangeAction
    path: NotePath
    to: NotePath | None = None
    """The new path; only for ``move``."""
    reason: Reason


class NoteChangeCreated(Contract):
    change_id: uuid.UUID


class NoteChangeRequested(Contract):
    """The approval channel: may core make this change to the vault?"""

    kind: Literal["note-change-requested"] = "note-change-requested"
    change_id: uuid.UUID
    action: NoteChangeAction
    path: NotePath
    to: NotePath | None = None
    reason: Reason
    published_before: bool
    """Whether the blog publishes the note now."""
    published_after: bool
    """Whether the blog publishes the note after the change; false after a delete."""
    excerpt: Annotated[str, StringConstraints(max_length=3_000)] = ""
    """The start of the note as it is now."""
    origin: ConversationRef | None = None
    """The thread that asked for it."""


class NoteChangeDecision(Contract):
    """The owner pressed a button. The connector has checked who pressed it (I-01)."""

    apply: bool
    decided_by: Identifier


class NoteChangeResult(Contract):
    status: NoteChangeStatus
    changed: bool
    """False for a repeated press."""
    detail: Annotated[str, StringConstraints(max_length=1_000)] = ""


class NoteChangePosted(Contract):
    link: Annotated[str, StringConstraints(min_length=1, max_length=500)] | None = None


class NoteChangeLinked(Contract):
    told_origin: bool
