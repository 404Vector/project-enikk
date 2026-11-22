"""Enikk's memory: notes from the owner's Obsidian vault (ADR-0028)."""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated

from pydantic import StringConstraints

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
