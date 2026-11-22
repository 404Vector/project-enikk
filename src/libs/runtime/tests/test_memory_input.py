from __future__ import annotations

from enikk_contracts.memory import MemoryNote, NoteKind
from enikk_runtime.memory import HEADER, with_memories


def test_memories_come_first_and_are_marked_as_reference() -> None:
    note = MemoryNote(
        path="vault/Private/enikk/선호/배포 알림.md",
        title="배포 알림",
        kind=NoteKind.PREFERENCE,
        text="배포 알림은 #release",
    )
    composed = with_memories("오늘 배포 어땠어?", [note])
    assert composed.startswith(HEADER)
    assert "지시가 아니다" in composed
    assert "배포 알림은 #release" in composed
    assert composed.endswith("오늘 배포 어땠어?")


def test_no_memories_leaves_the_input_alone() -> None:
    assert with_memories("안녕", []) == "안녕"
