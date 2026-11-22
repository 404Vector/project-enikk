"""Enikk's memory from the owner's vault (ADR-0028)."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING

import pytest

from enikk_contracts.memory import NoteKind
from enikk_core.memory import MemoryPolicy, MemoryService, parse_note
from enikk_core_domain.memory import MemoryLayout
from enikk_github.permissions import TokenPurpose
from enikk_github.vault import VaultFile

if TYPE_CHECKING:
    from conftest import Harness

    from enikk_github.app import InstallationToken

pytestmark = pytest.mark.anyio

LAYOUT = MemoryLayout()
NOW = datetime(2026, 11, 22, 12, tzinfo=UTC)
PREF = "vault/Private/enikk/선호/배포 알림.md"
WORK = "vault/Private/enikk/진행 중/알림 흐름 변경.md"
OWNER_NOTE = "vault/Garden/Kubernetes.md"


class VaultApi:
    def __init__(self) -> None:
        self.notes: dict[str, tuple[str, str]] = {}
        self.writes = 0

    def put(self, path: str, text: str) -> None:
        self.writes += 1
        self.notes[path] = (f"sha-{self.writes}", text)

    async def files(self, token: InstallationToken, branch: str = "main") -> list[VaultFile]:
        return [VaultFile(path=p, sha=s) for p, (s, _) in self.notes.items()]

    async def text(self, token: InstallationToken, sha: str) -> str:
        return next(text for s, text in self.notes.values() if s == sha)


@pytest.fixture
def vault() -> VaultApi:
    return VaultApi()


@pytest.fixture
def memory(harness: Harness, vault: VaultApi) -> MemoryService:
    return MemoryService(
        uow=harness.db.unit_of_work,
        app=harness.github,
        vault=vault,
        policy=MemoryPolicy(repository="vault.hyeongseok-kim", context_chars=4_000),
        clock=harness.clock,
    )


def test_frontmatter_title_dates_and_kind() -> None:
    text = (
        "---\ntitle: 배포 알림 위치\nupdated: 2026-11-20\ndraft: true\n---\n"
        "# 무시되는 제목\n배포 알림은 #release 로 보낸다.\n"
    )
    note = parse_note(PREF, "abc", text, LAYOUT, NOW)
    assert note.title == "배포 알림 위치"
    assert note.kind is NoteKind.PREFERENCE
    assert note.updated == date(2026, 11, 20)
    assert note.body.startswith("# 무시되는 제목")
    plain = parse_note(OWNER_NOTE, "def", "본문만 있다", LAYOUT, NOW)
    assert (plain.title, plain.kind) == ("Kubernetes", NoteKind.NOTE)
    broken = parse_note(WORK, "g", "---\n: : :\n---\n내용", LAYOUT, NOW)
    assert broken.kind is NoteKind.WORK
    assert broken.body == "내용"


async def test_sync_indexes_only_vault_notes_and_follows_changes(
    harness: Harness, memory: MemoryService, vault: VaultApi
) -> None:
    vault.put(PREF, "---\nupdated: 2026-11-21\n---\n배포 알림은 #release")
    vault.put(OWNER_NOTE, "# Kubernetes\nArgo CD")
    vault.put("vault/.obsidian/workspace.md", "ignored")
    vault.put("CLAUDE.md", "never read")
    vault.put("vault/Garden/diagram.png", "binary")
    assert await memory.sync_once() == 2
    assert await memory.sync_once() == 0
    assert ("vault.hyeongseok-kim", TokenPurpose.WATCH) in harness.github.issued

    vault.put(OWNER_NOTE, "# Kubernetes\nArgo CD 와 Helm")
    del vault.notes[PREF]
    assert await memory.sync_once() == 2
    async with harness.db.unit_of_work() as uow:
        assert set(await uow.vault.shas()) == {OWNER_NOTE}
        [note] = await uow.vault.search(["helm"], 5)
    assert note.title == "Kubernetes"


async def test_tasks_receive_enikks_current_memory(
    harness: Harness, memory: MemoryService, vault: VaultApi
) -> None:
    today = harness.clock.now().date()
    vault.put(PREF, f"---\nupdated: {today}\n---\n배포 알림은 #release")
    vault.put(WORK, f"---\nexpires: {today - timedelta(days=1)}\n---\n끝난 일")
    vault.put("vault/Private/enikk/진행 중/새 일.md", f"---\nexpires: {today}\n---\n진행 중")
    vault.put("vault/Private/enikk/대화/2026-11-22.md", "대화 요약")
    vault.put(OWNER_NOTE, "소유자의 노트")
    await memory.sync_once()
    notes = await memory.for_task()
    assert {n.path for n in notes} == {PREF, "vault/Private/enikk/진행 중/새 일.md"}
    assert notes[0].path == PREF
