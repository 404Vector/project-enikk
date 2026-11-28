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

    async def current_sha(self, token: InstallationToken, path: str, branch: str) -> str | None:
        found = self.vault.notes.get(path)
        return None if found is None else found[0]


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


# ---------------------------------------------------------------- the blog (ADR-0049)

POST = "vault/Garden/Engineering/꼬리 지연.md"
DRAFT_POST = "---\ntitle: 꼬리 지연\ndraft: true\n---\n# 꼬리 지연\n\n본문\n"
PUBLISHED = "---\ntitle: Kubernetes\n---\n# Kubernetes\n\n소유자의 글\n"


def test_public_paths_leave_out_what_the_blog_hides() -> None:
    assert LAYOUT.publishable("vault/Garden/Engineering/AWS.md")
    assert LAYOUT.publishable("vault/Blog.md")
    for path in (
        "vault/Private/enikk/선호/x.md",
        "vault/Project/private/x.md",
        "vault/templates/x.md",
        "vault/.obsidian/x.md",
        "vault/Garden/../Private/x.md",
        "vault/Garden/.hidden/x.md",
        "vault/Garden//x.md",
        "vault/Garden/x.png",
        "docs/x.md",
    ):
        assert not LAYOUT.publishable(path), path


async def test_a_new_post_is_written_as_a_draft_and_read_back(
    harness: Harness, keeper: MemoryService, vault: VaultApi, writer: FakeWriter
) -> None:
    import uuid

    from enikk_contracts.memory import NoteRead, NoteWrite

    _, work = await _owner_task(harness)
    lease = uuid.uuid4()
    written = await keeper.write_note(
        work.id,  # type: ignore[attr-defined]
        NoteWrite(lease_id=lease, path=POST, text=DRAFT_POST),
    )
    assert (written.action, written.draft) == ("created", True)
    assert vault.notes[POST][1] == DRAFT_POST
    assert writer.messages[-1].endswith("[skip ci]")
    assert ("vault.hyeongseok-kim", TokenPurpose.MEMORY) in harness.github.issued
    read = await keeper.read_note(NoteRead(lease_id=lease, path=POST))
    assert (read.exists, read.text) == (True, DRAFT_POST)
    missing = await keeper.read_note(NoteRead(lease_id=lease, path="vault/Garden/없음.md"))
    assert not missing.exists
    async with harness.db.unit_of_work() as uow:
        [found] = await uow.vault.search(["본문"], 5)
    assert found.path == POST


async def test_the_owners_published_note_can_be_edited_but_not_unpublished(
    harness: Harness, keeper: MemoryService, vault: VaultApi, writer: FakeWriter
) -> None:
    import uuid

    from enikk_contracts.memory import NoteWrite
    from enikk_core_domain.errors import PermissionDeniedError

    _, work = await _owner_task(harness)
    vault.put(OWNER_NOTE, PUBLISHED)
    edited = PUBLISHED.replace("소유자의 글", "고친 글")
    written = await keeper.write_note(
        work.id,  # type: ignore[attr-defined]
        NoteWrite(lease_id=uuid.uuid4(), path=OWNER_NOTE, text=edited),
    )
    assert (written.action, written.draft) == ("updated", False)
    assert vault.notes[OWNER_NOTE][1] == edited
    assert "[skip ci]" not in writer.messages[-1]
    hidden = edited.replace("title: Kubernetes\n", "title: Kubernetes\ndraft: true\n")
    with pytest.raises(PermissionDeniedError, match="draft"):
        await keeper.write_note(
            work.id,  # type: ignore[attr-defined]
            NoteWrite(lease_id=uuid.uuid4(), path=OWNER_NOTE, text=hidden),
        )
    assert vault.notes[OWNER_NOTE][1] == edited


async def test_public_writes_refuse_publishing_hidden_folders_secrets_and_others(
    harness: Harness, keeper: MemoryService, vault: VaultApi
) -> None:
    import uuid

    from enikk_contracts.memory import NoteWrite
    from enikk_core_domain.errors import InvalidRequestError, PermissionDeniedError

    _, work = await _owner_task(harness)
    vault.put(POST, DRAFT_POST)

    async def write(path: str, text: str, task: object = work) -> None:
        await keeper.write_note(
            task.id,  # type: ignore[attr-defined]
            NoteWrite(lease_id=uuid.uuid4(), path=path, text=text),
        )

    with pytest.raises(PermissionDeniedError, match="draft"):
        await write("vault/Garden/새 글.md", "# 새 글\n\n바로 공개\n")
    with pytest.raises(PermissionDeniedError, match="draft"):
        await write(POST, "# 꼬리 지연\n\n공개로 바꿈\n")
    with pytest.raises(PermissionDeniedError):
        await write("vault/Private/enikk/선호/x.md", DRAFT_POST)
    with pytest.raises(InvalidRequestError):
        await write(POST, DRAFT_POST + "ghp_" + "a" * 36)
    _, smoke = await _owner_task(harness, "smoke")
    with pytest.raises(PermissionDeniedError):
        await write(POST, DRAFT_POST, smoke)
    assert vault.notes[POST][1] == DRAFT_POST
    assert "vault/Garden/새 글.md" not in vault.notes


def test_drafts_follow_the_blogs_rule() -> None:
    from enikk_core.memory import is_draft

    assert is_draft(DRAFT_POST)
    assert is_draft("---\ndraft: 'true'\n---\nx")
    assert not is_draft(PUBLISHED)
    assert not is_draft("draft: true\n")
    assert not is_draft("---\ndraft: [\n---\nx")


# ------------------------------------------------------- reference notes (ADR-0050)

REFERENCE = "vault/Private/enikk/자료/NIKKE/엘리시온/Best Seller/레이블.md"


def test_reference_paths_stay_in_enikks_reference_folder() -> None:
    assert LAYOUT.is_reference(REFERENCE)
    assert LAYOUT.is_reference("vault/Private/enikk/자료/NIKKE.md")
    for path in (
        "vault/Private/enikk/사실/x.md",
        "vault/Private/enikk/자료/../선호/x.md",
        "vault/Private/enikk/자료/.hidden/x.md",
        "vault/Private/enikk/자료//x.md",
        "vault/Private/enikk/자료/x.png",
        "vault/Private/other/자료/x.md",
    ):
        assert not LAYOUT.is_reference(path), path
    assert LAYOUT.kind_of(REFERENCE) is NoteKind.NOTE


async def test_reference_notes_are_written_as_drafts_and_never_handed_to_tasks(
    harness: Harness, keeper: MemoryService, vault: VaultApi, writer: FakeWriter
) -> None:
    import uuid

    from enikk_contracts.memory import NoteWrite
    from enikk_core_domain.errors import PermissionDeniedError

    _, work = await _owner_task(harness)
    written = await keeper.write_note(
        work.id,  # type: ignore[attr-defined]
        NoteWrite(lease_id=uuid.uuid4(), path=REFERENCE, text=DRAFT_POST),
    )
    assert (written.action, written.draft) == ("created", True)
    assert writer.messages[-1].startswith("reference: created")
    assert writer.messages[-1].endswith("[skip ci]")
    assert await keeper.for_task() == ()
    async with harness.db.unit_of_work() as uow:
        [found] = await uow.vault.search(["본문"], 5)
    assert found.path == REFERENCE
    with pytest.raises(PermissionDeniedError, match="draft"):
        await keeper.write_note(
            work.id,  # type: ignore[attr-defined]
            NoteWrite(
                lease_id=uuid.uuid4(), path=REFERENCE.replace("레이블", "팬텀"), text="# x\n"
            ),
        )
    with pytest.raises(PermissionDeniedError):
        await keeper.write_note(
            work.id,  # type: ignore[attr-defined]
            NoteWrite(lease_id=uuid.uuid4(), path="vault/Private/enikk/선호/x.md", text=DRAFT_POST),
        )


# ------------------------------------------------- the owner's unpublished notes (ADR-0051)

PRIVATE_NOTE = "vault/Private/일기/2026-11-28.md"


def test_private_paths_are_the_owners_unpublished_notes() -> None:
    assert LAYOUT.private(PRIVATE_NOTE)
    assert LAYOUT.private("vault/Project/private/x.md")
    assert LAYOUT.private("vault/templates/주간.md")
    for path in (
        "vault/Private/enikk/선호/x.md",
        "vault/Private/enikk/자료/NIKKE/NIKKE.md",
        "vault/Garden/x.md",
        "vault/Private/../Garden/x.md",
        "vault/.obsidian/x.md",
        "vault/Private/.hidden/x.md",
    ):
        assert not LAYOUT.private(path), path


def _granted_keeper(harness: Harness, vault: VaultApi, writer: FakeWriter) -> MemoryService:
    from enikk_contracts.agents import PlatformTool, ToolPolicy
    from enikk_core_domain.testing import StaticAgentCatalog, agent
    from enikk_kernel.ids import RandomIdGenerator

    keeper_agent = agent("assistant").model_copy(
        update={"tools": ToolPolicy(platform=(PlatformTool.VAULT_PRIVATE,))}
    )
    return MemoryService(
        uow=harness.db.unit_of_work,
        app=harness.github,
        vault=vault,
        policy=MemoryPolicy(repository="vault.hyeongseok-kim"),
        clock=harness.clock,
        writer=writer,
        ids=RandomIdGenerator(),
        agents=StaticAgentCatalog(keeper_agent),
        owner="U0OWNER",
    )


async def test_a_granted_role_writes_the_owners_private_notes(
    harness: Harness, vault: VaultApi, writer: FakeWriter
) -> None:
    import uuid

    from enikk_contracts.memory import NoteWrite
    from enikk_core_domain.errors import PermissionDeniedError

    keeper = _granted_keeper(harness, vault, writer)
    _, work = await _owner_task(harness)
    written = await keeper.write_note(
        work.id,  # type: ignore[attr-defined]
        NoteWrite(lease_id=uuid.uuid4(), path=PRIVATE_NOTE, text="# 일기\n\n오늘\n"),
    )
    assert (written.action, written.draft) == ("created", True)
    assert writer.messages[-1].startswith("notes: created")
    assert writer.messages[-1].endswith("[skip ci]")
    with pytest.raises(PermissionDeniedError):
        await keeper.write_note(
            work.id,  # type: ignore[attr-defined]
            NoteWrite(lease_id=uuid.uuid4(), path=PREF, text="---\ndraft: true\n---\nx"),
        )


async def test_other_roles_may_not_write_the_owners_private_notes(
    harness: Harness, keeper: MemoryService, vault: VaultApi
) -> None:
    import uuid

    from enikk_contracts.memory import NoteWrite
    from enikk_core_domain.errors import PermissionDeniedError

    _, work = await _owner_task(harness)
    with pytest.raises(PermissionDeniedError, match="unpublished"):
        await keeper.write_note(
            work.id,  # type: ignore[attr-defined]
            NoteWrite(lease_id=uuid.uuid4(), path=PRIVATE_NOTE, text="# 일기\n"),
        )
    assert PRIVATE_NOTE not in vault.notes


# ------------------------------------------------------ note changes (ADR-0051 §4)


def test_set_draft_adds_and_removes_the_flag() -> None:
    from enikk_core.memory import is_draft, set_draft

    published = set_draft(DRAFT_POST, draft=False)
    assert not is_draft(published)
    assert "title: 꼬리 지연" in published
    assert published.endswith("# 꼬리 지연\n\n본문\n")
    assert is_draft(set_draft(published, draft=True))
    assert is_draft(set_draft("# 제목\n", draft=True))
    assert set_draft("# 제목\n", draft=False) == "# 제목\n"


async def _ask(keeper: MemoryService, work: object, **fields: object) -> object:
    import uuid

    from enikk_contracts.memory import NoteChangeRequest

    request = NoteChangeRequest(lease_id=uuid.uuid4(), reason="이유", **fields)  # type: ignore[arg-type]
    return await keeper.request_change(work.id, request)  # type: ignore[attr-defined]


async def test_publishing_waits_for_the_owner_then_removes_the_draft_flag(
    harness: Harness, vault: VaultApi, writer: FakeWriter
) -> None:
    from enikk_contracts.memory import (
        NoteChangeAction,
        NoteChangeDecision,
        NoteChangeRequested,
        NoteChangeStatus,
    )
    from enikk_core.memory import is_draft
    from enikk_core_domain.errors import PermissionDeniedError

    keeper = _granted_keeper(harness, vault, writer)
    _, work = await _owner_task(harness)
    vault.put(POST, DRAFT_POST)
    created = await _ask(keeper, work, action=NoteChangeAction.PUBLISH, path=POST)
    assert vault.notes[POST][1] == DRAFT_POST
    async with harness.db.unit_of_work() as uow:
        [notice] = await uow.notices.pending(10)
    body = notice.body
    assert isinstance(body, NoteChangeRequested)
    assert (body.published_before, body.published_after) == (False, True)
    change_id = created.change_id  # type: ignore[attr-defined]
    with pytest.raises(PermissionDeniedError):
        await keeper.decide_change(change_id, NoteChangeDecision(apply=True, decided_by="U0X"))
    result = await keeper.decide_change(
        change_id, NoteChangeDecision(apply=True, decided_by="U0OWNER")
    )
    assert result.status is NoteChangeStatus.DONE
    assert not is_draft(vault.notes[POST][1])
    assert writer.messages[-1] == "blog: publish 꼬리 지연"
    again = await keeper.decide_change(
        change_id, NoteChangeDecision(apply=True, decided_by="U0OWNER")
    )
    assert not again.changed
    async with harness.db.unit_of_work() as uow:
        from enikk_contracts.common import ConnectorKind

        told = await uow.outbox.pending(ConnectorKind.SLACK, 10)
    assert any("발행했습니다" in m.text for m in told)


async def test_a_note_changed_after_the_request_is_left_alone(
    harness: Harness, vault: VaultApi, writer: FakeWriter
) -> None:
    from enikk_contracts.memory import NoteChangeAction, NoteChangeDecision, NoteChangeStatus

    keeper = _granted_keeper(harness, vault, writer)
    _, work = await _owner_task(harness)
    vault.put(OWNER_NOTE, PUBLISHED)
    created = await _ask(keeper, work, action=NoteChangeAction.DELETE, path=OWNER_NOTE)
    vault.put(OWNER_NOTE, PUBLISHED + "\n덧붙임\n")
    result = await keeper.decide_change(
        created.change_id,  # type: ignore[attr-defined]
        NoteChangeDecision(apply=True, decided_by="U0OWNER"),
    )
    assert result.status is NoteChangeStatus.STALE
    assert OWNER_NOTE in vault.notes


async def test_move_delete_and_reject(
    harness: Harness, vault: VaultApi, writer: FakeWriter
) -> None:
    from enikk_contracts.memory import NoteChangeAction, NoteChangeDecision, NoteChangeStatus

    keeper = _granted_keeper(harness, vault, writer)
    _, work = await _owner_task(harness)
    vault.put(PRIVATE_NOTE, "# 일기\n")
    moved_to = "vault/Private/일기/옮김.md"
    created = await _ask(keeper, work, action=NoteChangeAction.MOVE, path=PRIVATE_NOTE, to=moved_to)
    done = await keeper.decide_change(
        created.change_id,  # type: ignore[attr-defined]
        NoteChangeDecision(apply=True, decided_by="U0OWNER"),
    )
    assert done.status is NoteChangeStatus.DONE
    assert PRIVATE_NOTE not in vault.notes
    assert vault.notes[moved_to][1] == "# 일기\n"
    assert writer.messages[-1].endswith("[skip ci]")
    refused = await _ask(keeper, work, action=NoteChangeAction.DELETE, path=moved_to)
    rejected = await keeper.decide_change(
        refused.change_id,  # type: ignore[attr-defined]
        NoteChangeDecision(apply=False, decided_by="U0OWNER"),
    )
    assert rejected.status is NoteChangeStatus.REJECTED
    assert moved_to in vault.notes
    deleted = await _ask(keeper, work, action=NoteChangeAction.DELETE, path=moved_to)
    await keeper.decide_change(
        deleted.change_id,  # type: ignore[attr-defined]
        NoteChangeDecision(apply=True, decided_by="U0OWNER"),
    )
    assert moved_to not in vault.notes


async def test_change_requests_are_checked_before_asking(
    harness: Harness, keeper: MemoryService, vault: VaultApi, writer: FakeWriter
) -> None:
    from enikk_contracts.memory import NoteChangeAction
    from enikk_core_domain.errors import (
        InvalidRequestError,
        NotFoundError,
        PermissionDeniedError,
    )

    granted = _granted_keeper(harness, vault, writer)
    _, work = await _owner_task(harness)
    vault.put(OWNER_NOTE, PUBLISHED)
    vault.put(POST, DRAFT_POST)
    with pytest.raises(PermissionDeniedError):
        await _ask(keeper, work, action=NoteChangeAction.DELETE, path=OWNER_NOTE)
    with pytest.raises(InvalidRequestError, match="already published"):
        await _ask(granted, work, action=NoteChangeAction.PUBLISH, path=OWNER_NOTE)
    with pytest.raises(InvalidRequestError, match="not published"):
        await _ask(granted, work, action=NoteChangeAction.UNPUBLISH, path=POST)
    with pytest.raises(InvalidRequestError, match="already a note"):
        await _ask(granted, work, action=NoteChangeAction.MOVE, path=POST, to=OWNER_NOTE)
    with pytest.raises(PermissionDeniedError):
        await _ask(granted, work, action=NoteChangeAction.DELETE, path=PREF)
    with pytest.raises(NotFoundError):
        await _ask(granted, work, action=NoteChangeAction.DELETE, path="vault/Garden/없음.md")
    with pytest.raises(InvalidRequestError):
        await _ask(granted, work, action=NoteChangeAction.DELETE, path=POST, to=OWNER_NOTE)
