"""Enikk's memory from the owner's vault (ADR-0028)."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING

import pytest

from enikk_contracts.memory import NoteKind
from enikk_contracts.outbox import OutboundKind
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


class FakeWriter:
    def __init__(self, vault: VaultApi) -> None:
        self.vault = vault
        self.messages: list[str] = []

    async def put(
        self, token: InstallationToken, path: str, text: str, message: str, branch: str = "main"
    ) -> str:
        self.messages.append(message)
        self.vault.put(path, text)
        return self.vault.notes[path][0]

    async def delete(
        self, token: InstallationToken, path: str, message: str, branch: str = "main"
    ) -> bool:
        self.messages.append(message)
        return self.vault.notes.pop(path, None) is not None


async def _owner_task(harness: Harness, connector: str = "slack") -> tuple[object, object]:
    from enikk_contracts.common import ConnectorKind, ConversationRef
    from enikk_core_domain.model import Session
    from enikk_core_domain.testing import T0, agent, task

    session = Session.start(
        session_id=__import__("uuid").uuid4(),
        conversation=ConversationRef(
            connector=ConnectorKind(connector), channel="D1", thread="1.0"
        ),
        agent=agent("assistant"),
        now=T0,
    )
    work = task(session)
    async with harness.db.unit_of_work() as uow:
        await uow.sessions.add(session)
        await uow.tasks.add(work)
        await uow.commit()
    return session, work


@pytest.fixture
def writer(vault: VaultApi) -> FakeWriter:
    return FakeWriter(vault)


@pytest.fixture
def keeper(harness: Harness, vault: VaultApi, writer: FakeWriter) -> MemoryService:
    from enikk_kernel.ids import RandomIdGenerator

    return MemoryService(
        uow=harness.db.unit_of_work,
        app=harness.github,
        vault=vault,
        policy=MemoryPolicy(repository="vault.hyeongseok-kim"),
        clock=harness.clock,
        writer=writer,
        ids=RandomIdGenerator(),
    )


async def test_remember_writes_a_private_draft_note_and_tells_the_thread(
    harness: Harness, keeper: MemoryService, vault: VaultApi, writer: FakeWriter
) -> None:
    import uuid

    from enikk_contracts.common import ConnectorKind
    from enikk_contracts.memory import RememberRequest

    _, work = await _owner_task(harness)
    written = await keeper.remember(
        work.id,  # type: ignore[attr-defined]
        RememberRequest(
            lease_id=uuid.uuid4(),
            kind=NoteKind.PREFERENCE,
            title="배포 알림 / 위치",
            text="배포 알림은 #release 로",
        ),
    )
    assert written.path == "vault/Private/enikk/선호/배포 알림 위치.md"
    text = vault.notes[written.path][1]
    assert "\ndraft: true\n" in text
    assert "kind: preference" in text
    assert writer.messages[-1].endswith("[skip ci]")
    assert ("vault.hyeongseok-kim", TokenPurpose.MEMORY) in harness.github.issued
    [note] = await keeper.for_task()
    assert note.text.endswith("배포 알림은 #release 로")
    async with harness.db.unit_of_work() as uow:
        [told] = await uow.outbox.pending(ConnectorKind.SLACK, 5)
    assert told.text.startswith("기억했습니다: 배포 알림 / 위치")
    assert told.kind is OutboundKind.REMEMBERED


async def test_notes_name_the_agent_by_its_current_name(
    harness: Harness, vault: VaultApi, writer: FakeWriter
) -> None:
    import uuid

    from enikk_contracts.memory import RememberRequest
    from enikk_core_domain.testing import T0, StaticAgentCatalog, agent
    from enikk_kernel.ids import RandomIdGenerator

    enikk = agent("enikk").model_copy(update={"aliases": ("assistant",)})
    keeper = MemoryService(
        uow=harness.db.unit_of_work,
        app=harness.github,
        vault=vault,
        policy=MemoryPolicy(repository="vault.hyeongseok-kim"),
        clock=harness.clock,
        writer=writer,
        ids=RandomIdGenerator(),
        agents=StaticAgentCatalog(enikk),
    )
    _, work = await _owner_task(harness)  # a thread recorded under the former name
    written = await keeper.remember(
        work.id,  # type: ignore[attr-defined]
        RememberRequest(lease_id=uuid.uuid4(), kind=NoteKind.FACT, title="사실", text="내용"),
    )
    assert "\nby: enikk\n" in vault.notes[written.path][1]
    path = await keeper.write_conversations(T0.date(), "대화", "요약", 1)
    assert "\nby: enikk\n" in vault.notes[path][1]


async def test_work_expires_and_can_be_forgotten(
    harness: Harness, keeper: MemoryService, vault: VaultApi
) -> None:
    import uuid

    from enikk_contracts.memory import ForgetRequest, RememberRequest, UpdateMemoryRequest

    _, work = await _owner_task(harness)
    lease = uuid.uuid4()
    written = await keeper.remember(
        work.id,  # type: ignore[attr-defined]
        RememberRequest(lease_id=lease, kind=NoteKind.WORK, title="알림 흐름", text="진행 중"),
    )
    expires = (harness.clock.now().date() + timedelta(days=14)).isoformat()
    assert f"expires: '{expires}'" in vault.notes[written.path][1]
    await keeper.update(
        work.id,  # type: ignore[attr-defined]
        UpdateMemoryRequest(lease_id=lease, path=written.path, text="PR #47 머지됨"),
    )
    assert "PR #47 머지됨" in vault.notes[written.path][1]
    await keeper.forget(work.id, ForgetRequest(lease_id=lease, path=written.path))  # type: ignore[attr-defined]
    assert written.path not in vault.notes
    assert await keeper.for_task() == ()


async def test_writes_outside_enikks_folder_secrets_and_non_owner_work_are_refused(
    harness: Harness, keeper: MemoryService, vault: VaultApi
) -> None:
    import uuid

    from enikk_contracts.memory import ForgetRequest, RememberRequest
    from enikk_core_domain.errors import (
        InvalidRequestError,
        NotFoundError,
        PermissionDeniedError,
    )

    _, work = await _owner_task(harness)
    vault.put(OWNER_NOTE, "소유자의 노트")
    await keeper.sync_once()
    with pytest.raises(PermissionDeniedError):
        await keeper.forget(work.id, ForgetRequest(lease_id=uuid.uuid4(), path=OWNER_NOTE))  # type: ignore[attr-defined]
    with pytest.raises(NotFoundError):
        await keeper.forget(
            work.id,  # type: ignore[attr-defined]
            ForgetRequest(lease_id=uuid.uuid4(), path="vault/Private/enikk/선호/없음.md"),
        )
    secret = "ghp_" + "a" * 36
    with pytest.raises(InvalidRequestError):
        await keeper.remember(
            work.id,  # type: ignore[attr-defined]
            RememberRequest(lease_id=uuid.uuid4(), kind=NoteKind.FACT, title="키", text=secret),
        )
    _, smoke = await _owner_task(harness, "smoke")
    with pytest.raises(PermissionDeniedError):
        await keeper.remember(
            smoke.id,  # type: ignore[attr-defined]
            RememberRequest(lease_id=uuid.uuid4(), kind=NoteKind.FACT, title="x", text="y"),
        )
    assert OWNER_NOTE in vault.notes


def test_enikks_folder_must_stay_private() -> None:
    with pytest.raises(ValueError, match="Private"):
        MemoryPolicy(repository="v", layout=MemoryLayout(enikk_dir="vault/Garden/enikk/"))


async def test_recall_searches_the_whole_vault(
    harness: Harness, keeper: MemoryService, vault: VaultApi
) -> None:
    import uuid

    from enikk_contracts.memory import MemorySearch

    vault.put(OWNER_NOTE, "# Kubernetes\n" + "x " * 300 + "Argo CD 는 GitOps 도구다")
    await keeper.sync_once()
    found = await keeper.search(MemorySearch(lease_id=uuid.uuid4(), query="argo gitops"))
    [match] = found.matches
    assert match.path == OWNER_NOTE
    assert "Argo CD" in match.snippet
    assert match.snippet.startswith("…")
