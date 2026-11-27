"""Enikk's memory as indexed from the owner's vault (ADR-0028).

The vault repository is the source; core keeps an index of its notes so it can hand
a short list to each task and search the rest. Enikk's own notes live under one
folder, sorted into kinds by sub-folder.
"""

from __future__ import annotations

from datetime import date
from typing import Annotated, Final

from pydantic import AwareDatetime, Field

from enikk_contracts.memory import MemoryNote, NoteKind
from enikk_kernel.models import FrozenModel

MAX_BODY: Final = 20_000
"""Characters of a note's body kept in the index; longer notes are cut."""
KIND_DIRS: Final = {
    "선호": NoteKind.PREFERENCE,
    "사실": NoteKind.FACT,
    "진행 중": NoteKind.WORK,
    "대화": NoteKind.CONVERSATION,
}
CONTEXT_KINDS: Final = frozenset({NoteKind.PREFERENCE, NoteKind.FACT, NoteKind.WORK})
REFERENCE_FOLDER: Final = "자료"
"""Enikk's reference notes, in any sub-folders: searched and read, never handed to every
task (ADR-0050)."""
UNPUBLISHED_DIRS: Final = frozenset({"Private", "private", "templates", ".obsidian"})
"""Folders the blog leaves out (the vault's Quartz ``ignorePatterns``), at any depth."""


class VaultNote(FrozenModel):
    path: Annotated[str, Field(min_length=1, max_length=500)]
    blob_sha: Annotated[str, Field(min_length=1, max_length=64)]
    title: Annotated[str, Field(max_length=300)]
    kind: NoteKind
    body: Annotated[str, Field(max_length=MAX_BODY)]
    updated: date | None = None
    expires: date | None = None
    synced_at: AwareDatetime

    def expired(self, today: date) -> bool:
        return self.expires is not None and self.expires < today


class MemoryLayout(FrozenModel):
    """Where notes are: everything under ``notes_root``; enikk's own under ``enikk_dir``."""

    notes_root: str = "vault/"
    enikk_dir: str = "vault/Private/enikk/"
    excluded: tuple[str, ...] = ("vault/.obsidian/",)

    def readable(self, path: str) -> bool:
        return (
            path.startswith(self.notes_root)
            and path.endswith(".md")
            and not any(path.startswith(e) for e in self.excluded)
        )

    def kind_of(self, path: str) -> NoteKind:
        if not path.startswith(self.enikk_dir):
            return NoteKind.NOTE
        folder = path.removeprefix(self.enikk_dir).split("/", 1)[0]
        return KIND_DIRS.get(folder, NoteKind.NOTE)

    def is_enikks(self, path: str) -> bool:
        return path.startswith(self.enikk_dir)

    @property
    def reference_dir(self) -> str:
        return f"{self.enikk_dir}{REFERENCE_FOLDER}/"

    def is_reference(self, path: str) -> bool:
        """A reference note: a Markdown file under enikk's reference folder, with no
        hidden or relative parts."""
        if not path.startswith(self.reference_dir) or not path.endswith(".md"):
            return False
        parts = path.removeprefix(self.reference_dir).split("/")
        return all(p and not p.startswith(".") for p in parts)

    def publishable(self, path: str) -> bool:
        """A note the blog can publish: under ``notes_root``, outside every folder the
        blog leaves out, with no hidden or relative parts."""
        if not self.readable(path):
            return False
        parts = path.removeprefix(self.notes_root).split("/")
        return all(p and not p.startswith(".") for p in parts) and not any(
            p in UNPUBLISHED_DIRS for p in parts[:-1]
        )


def context_notes(
    notes: list[VaultNote], layout: MemoryLayout, today: date, limit_chars: int
) -> tuple[MemoryNote, ...]:
    """Enikk's preferences, facts and unexpired work, most recently updated first,
    within ``limit_chars`` of text in total."""
    chosen = sorted(
        (
            n
            for n in notes
            if layout.is_enikks(n.path) and n.kind in CONTEXT_KINDS and not n.expired(today)
        ),
        key=lambda n: (n.updated or date.min, n.path),
        reverse=True,
    )
    picked: list[MemoryNote] = []
    budget = limit_chars
    for note in chosen:
        text = note.body.strip()
        if len(text) > 1_000:
            text = text[:999] + "…"
        cost = len(text) + len(note.title) + 8
        if cost > budget:
            continue
        budget -= cost
        picked.append(MemoryNote(path=note.path, title=note.title, kind=note.kind, text=text))
    return tuple(picked)
