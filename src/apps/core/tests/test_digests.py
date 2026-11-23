"""Daily conversation summaries in the owner's vault (ADR-0035)."""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING

import pytest

from enikk_contracts.agents import AgentDefinition, PlatformTool, PromptRef, ToolPolicy
from enikk_contracts.common import ConnectorKind, ConversationRef, Engine
from enikk_contracts.digests import DigestFailed, DigestRequest, DigestStatus
from enikk_contracts.outbox import OutboundKind
from enikk_contracts.tasks import (
    Failed,
    LeaseRequest,
    ResultReport,
    Succeeded,
    TaskStatus,
)
from enikk_core.digests import DigestPolicy, DigestService
from enikk_core.memory import MemoryPolicy, MemoryService
from enikk_core_domain.digests import DigestSchedule
from enikk_core_domain.errors import InvalidRequestError, PermissionDeniedError
from enikk_core_domain.model import OutboxMessage, Session, Task, WorkerScope
from enikk_core_domain.testing import agent
from enikk_kernel.ids import RandomIdGenerator
from enikk_kernel.redaction import MASK

if TYPE_CHECKING:
    from conftest import Harness

    from enikk_github.app import InstallationToken
    from enikk_github.vault import VaultFile

pytestmark = pytest.mark.anyio

ZONE = DigestSchedule().zone
DAY = date(2026, 11, 23)
PATH = "vault/Private/enikk/대화/2026-11-23.md"
SECRET = "xoxb-" + "1234567890-abcdefghij"


def kst(hour: int, minute: int = 0, day: int = 23) -> datetime:
    return datetime(2026, 11, day, hour, minute, tzinfo=ZONE).astimezone(UTC)


class Vault:
    def __init__(self) -> None:
        self.notes: dict[str, str] = {}
        self.writes: list[str] = []

    async def files(self, token: InstallationToken, branch: str = "main") -> list[VaultFile]:
        return []

    async def text(self, token: InstallationToken, sha: str) -> str:
        return ""

    async def put(
        self, token: InstallationToken, path: str, text: str, message: str, branch: str = "main"
    ) -> str:
        self.writes.append(path)
        self.notes[path] = text
        return f"sha-{len(self.writes)}"

    async def delete(
        self, token: InstallationToken, path: str, message: str, branch: str = "main"
    ) -> bool:
        return self.notes.pop(path, None) is not None


def summarizer() -> AgentDefinition:
    return AgentDefinition(
        name="summarizer",
        description="summarizer for tests",
        engine=Engine.CLAUDE,
        prompt=PromptRef(name="conversation-summary", label="production"),
        tools=ToolPolicy(platform_defaults=False),
    )


@pytest.fixture
def vault() -> Vault:
    return Vault()


@pytest.fixture
def digests(harness: Harness, vault: Vault) -> DigestService:
    harness.catalog.put(summarizer())
    memory = MemoryService(
        uow=harness.db.unit_of_work,
        app=harness.github,
        vault=vault,
        writer=vault,
        policy=MemoryPolicy(repository="vault.hyeongseok-kim"),
        clock=harness.clock,
        ids=RandomIdGenerator(),
    )
    return DigestService(
        uow=harness.db.unit_of_work,
        catalog=harness.catalog,
        memory=memory,
        clock=harness.clock,
        ids=RandomIdGenerator(),
        policy=DigestPolicy(schedule=DigestSchedule()),
    )


def at(harness: Harness, when: datetime) -> None:
    harness.clock.advance(when - harness.clock.now())


async def converse(
    harness: Harness, asked: str, answer: str, when: datetime, thread: str = "1.0"
) -> Task:
    """The owner asked in ``thread`` at ``when``; the assistant answered a minute later."""
    conversation = ConversationRef(connector=ConnectorKind.SLACK, channel="D1", thread=thread)
    async with harness.db.unit_of_work() as uow:
        session = await uow.sessions.find_by_conversation(conversation)
        if session is None:
            session = Session.start(
                session_id=uuid.uuid4(),
                conversation=conversation,
                agent=agent("assistant"),
                now=when,
            )
            await uow.sessions.add(session)
        lease = uuid.uuid4()
        work = (
            Task.enqueue(
                task_id=uuid.uuid4(),
                session=session,
                source_event_id=f"ev-{uuid.uuid4()}",
                text=asked,
                max_attempts=1,
                now=when,
            )
            .grant_lease(lease_id=lease, now=when, duration=timedelta(minutes=5))
            .succeed(lease_id=lease, text=answer, now=when)
        )
        await uow.tasks.add(work)
        await uow.outbox.add(
            OutboxMessage(
                id=uuid.uuid4(),
                task_id=work.id,
                source_event_id=work.source_event_id,
                conversation=conversation,
                kind=OutboundKind.REPLY,
                text=answer,
                created_at=when + timedelta(minutes=1),
            )
        )
        await uow.commit()
    return work


async def summarize(harness: Harness, output: str | None) -> Task:
    """The runtime takes the summarizer's task and reports ``output`` (None: a failure)."""
    lease = await harness.usecases.lease_task(Engine.CLAUDE, LeaseRequest(wait_seconds=0))
    assignment = lease.assignment
    assert assignment is not None
    assert assignment.definition.name == "summarizer"
    assert assignment.memories == ()
    outcome = (
        Failed(reason="prompt 'conversation-summary' is unavailable")
        if output is None
        else Succeeded(text=output)
    )
    await harness.usecases.report_result(
        WorkerScope.shared(Engine.CLAUDE),
        assignment.task_id,
        ResultReport(lease_id=assignment.lease_id, outcome=outcome),
    )
    async with harness.db.unit_of_work() as uow:
        task = await uow.tasks.get_for_update(assignment.task_id)
    assert task is not None
    return task


async def test_the_day_in_seoul_is_summarized_at_four_and_written_once(
    harness: Harness, digests: DigestService, vault: Vault
) -> None:
    await converse(harness, "어제 밤 이야기", "네", kst(23, 59, day=22))
    await converse(harness, "자정 직후 질문", "답 1", kst(0, 0))
    await converse(harness, "자기 전 질문", "답 2", kst(23, 58), thread="2.0")
    await converse(harness, "다음 날 질문", "답 3", kst(0, 0, day=24))

    at(harness, kst(3, 59, day=24))
    assert await digests.start_due() == [date(2026, 11, 21), date(2026, 11, 22)]
    at(harness, kst(4, 0, day=24))
    assert await digests.start_due() == [DAY]

    async with harness.db.unit_of_work() as uow:
        made = [t for t in await uow.tasks.created_between(kst(3), kst(5, day=24), 50)]
    [day22, day23] = [t.input for t in made if t.agent == "summarizer"]
    assert "어제 밤 이야기" in day22
    assert "자정 직후 질문" not in day22
    assert "자정 직후 질문" in day23
    assert "자기 전 질문" in day23
    assert "어제 밤 이야기" not in day23
    assert "다음 날 질문" not in day23
    assert "00:00 소유자" in day23

    await summarize(harness, "11월 22일 요약")
    done = await summarize(harness, "하루 요약: 대화 요약 기능을 논의함\n\n## 스레드 1\n- 주제")
    async with harness.db.unit_of_work() as uow:
        assert not await uow.outbox.pending(ConnectorKind.SCHEDULE, 10)
    assert done.status is TaskStatus.SUCCEEDED
    assert await digests.settle() == 2
    assert vault.writes == ["vault/Private/enikk/대화/2026-11-22.md", PATH]
    note = vault.notes[PATH]
    assert "\ndraft: true\n" in note
    assert "kind: conversation" in note
    assert "# 2026-11-23 대화\n\n하루 요약: 대화 요약 기능을 논의함" in note
    assert "- 스레드 2: slack D1 2.0 (assistant)" in note

    at(harness, kst(4, 30, day=24))
    await digests.run_once()
    at(harness, kst(4, 30, day=25))
    await digests.run_once()
    assert len(vault.writes) == 2


async def test_a_day_without_conversations_gets_no_file(
    harness: Harness, digests: DigestService, vault: Vault
) -> None:
    at(harness, kst(4, 0, day=24))
    assert DAY in await digests.start_due()
    await digests.run_once()
    assert vault.writes == []
    async with harness.db.unit_of_work() as uow:
        digest = await uow.digests.get_for_update(DAY)
        assert not await uow.queue.claim_next(Engine.CLAUDE)
    assert digest is not None
    assert digest.status is DigestStatus.EMPTY


async def test_asking_again_replaces_the_note_instead_of_adding_one(
    harness: Harness, digests: DigestService, vault: Vault
) -> None:
    owner = await converse(harness, "요약 다시 만들어 줘", "네", kst(10))
    harness.catalog.put(
        AgentDefinition(
            name="assistant",
            description="assistant",
            engine=Engine.CLAUDE,
            prompt=PromptRef(name="assistant", label="production"),
            tools=ToolPolicy(platform=(PlatformTool.CONVERSATION_DIGEST,)),
        )
    )
    at(harness, kst(4, 0, day=24))
    await digests.start_due()
    await summarize(harness, "첫 요약")
    await digests.settle()

    at(harness, kst(9, 0, day=24))
    request = DigestRequest(lease_id=uuid.uuid4(), day=DAY)
    accepted = await digests.request(owner.id, request)
    assert accepted.status is DigestStatus.RUNNING
    assert (await digests.request(owner.id, request)).status is DigestStatus.RUNNING
    await summarize(harness, "두 번째 요약")
    await digests.settle()
    assert vault.writes == [PATH, PATH]
    assert "두 번째 요약" in vault.notes[PATH]
    assert "첫 요약" not in vault.notes[PATH]

    with pytest.raises(InvalidRequestError):
        await digests.request(owner.id, DigestRequest(lease_id=uuid.uuid4(), day=date(2026, 12, 1)))


async def test_only_a_granted_role_in_the_owners_slack_may_ask(
    harness: Harness, digests: DigestService
) -> None:
    owner = await converse(harness, "요약해 줘", "네", kst(10))
    request = DigestRequest(lease_id=uuid.uuid4(), day=DAY)
    with pytest.raises(PermissionDeniedError, match="may not ask"):
        await digests.request(owner.id, request)


async def test_secrets_are_masked_in_the_input_and_in_the_note(
    harness: Harness, digests: DigestService, vault: Vault
) -> None:
    await converse(harness, f"슬랙 토큰 {SECRET} 등록했어", "확인했습니다", kst(10))
    at(harness, kst(4, 0, day=24))
    await digests.start_due()
    async with harness.db.unit_of_work() as uow:
        [task] = await uow.tasks.created_between(kst(4, 0, day=24), kst(5, day=24), 9)
    assert SECRET not in task.input
    assert MASK in task.input
    await summarize(harness, f"토큰 {SECRET} 를 등록함")
    await digests.settle()
    assert SECRET not in vault.notes[PATH]
    assert MASK in vault.notes[PATH]


async def test_a_failed_day_is_reported_and_tried_once_more_the_next_day(
    harness: Harness, digests: DigestService, vault: Vault
) -> None:
    await converse(harness, "질문", "답", kst(10))
    at(harness, kst(4, 0, day=24))
    await digests.start_due()
    await summarize(harness, None)
    await digests.settle()
    async with harness.db.unit_of_work() as uow:
        [notice] = await uow.notices.pending(10)
    assert isinstance(notice.body, DigestFailed)
    assert (notice.body.day, notice.body.retry) == (DAY, True)
    assert "conversation-summary" in notice.body.reason

    at(harness, kst(23, 0, day=24))
    assert DAY not in await digests.start_due()
    at(harness, kst(4, 0, day=25))
    assert DAY in await digests.start_due()
    await summarize(harness, None)
    await digests.settle()
    async with harness.db.unit_of_work() as uow:
        notices = await uow.notices.pending(10)
    assert [n.body.retry for n in notices if isinstance(n.body, DigestFailed)] == [True, False]

    at(harness, kst(4, 0, day=26))
    assert DAY not in await digests.start_due()
    async with harness.db.unit_of_work() as uow:
        assert await uow.queue.claim_next(Engine.CLAUDE) is None
    assert vault.writes == []
