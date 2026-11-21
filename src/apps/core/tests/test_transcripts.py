"""Transcript API (ADR-0014): only an active lease holder reaches its session's transcript."""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import TYPE_CHECKING

import pytest

from enikk_contracts.common import ConnectorKind, ConversationRef
from enikk_contracts.events import InboundEvent
from enikk_contracts.tasks import LeaseRequest, ResultReport, Succeeded, TaskAssignment
from enikk_contracts.transcripts import (
    SubpathQuery,
    TranscriptAppend,
    TranscriptEntry,
    TranscriptKey,
    TranscriptQuery,
)
from enikk_core_client import CoreConflictError, CoreForbiddenError, CoreNotFoundError

if TYPE_CHECKING:
    from conftest import Harness

    from enikk_core_client import CoreClient

pytestmark = pytest.mark.anyio

MAIN = TranscriptKey(project_key="-app", engine_session_id="s-1")
SUB = TranscriptKey(project_key="-app", engine_session_id="s-1", subpath="subagents/agent-a")
ENTRIES: tuple[TranscriptEntry, ...] = (
    {"uuid": "u1", "type": "user", "message": {"content": "hi"}},
    {"uuid": "u2", "type": "assistant", "message": {"content": [{"type": "text", "text": "hey"}]}},
)


async def _assignment(slack: CoreClient, claude: CoreClient, thread: str) -> TaskAssignment:
    await slack.submit_event(
        InboundEvent(
            event_id=f"Ev-{thread}",
            conversation=ConversationRef(
                connector=ConnectorKind.SLACK, channel="C1", thread=thread
            ),
            user="U1",
            text="hello",
            agent="assistant",
        )
    )
    response = await claude.lease_task(LeaseRequest(wait_seconds=0))
    assert response.assignment is not None
    return response.assignment


async def test_lease_holder_appends_and_loads(slack: CoreClient, claude: CoreClient) -> None:
    task = await _assignment(slack, claude, "1.0")
    appended = await claude.append_transcript(
        task.task_id, TranscriptAppend(lease_id=task.lease_id, key=MAIN, entries=ENTRIES)
    )
    assert appended.stored == 2
    again = await claude.append_transcript(
        task.task_id, TranscriptAppend(lease_id=task.lease_id, key=MAIN, entries=ENTRIES[1:])
    )
    assert again.stored == 0
    await claude.append_transcript(
        task.task_id, TranscriptAppend(lease_id=task.lease_id, key=SUB, entries=ENTRIES[:1])
    )

    loaded = await claude.load_transcript(
        task.task_id, TranscriptQuery(lease_id=task.lease_id, key=MAIN)
    )
    assert loaded.entries == ENTRIES
    subpaths = await claude.transcript_subpaths(
        task.task_id,
        SubpathQuery(lease_id=task.lease_id, project_key="-app", engine_session_id="s-1"),
    )
    assert subpaths.subpaths == ("subagents/agent-a",)


async def test_transcripts_are_scoped_to_the_session(slack: CoreClient, claude: CoreClient) -> None:
    first = await _assignment(slack, claude, "1.0")
    await claude.append_transcript(
        first.task_id, TranscriptAppend(lease_id=first.lease_id, key=MAIN, entries=ENTRIES)
    )
    other = await _assignment(slack, claude, "2.0")
    loaded = await claude.load_transcript(
        other.task_id, TranscriptQuery(lease_id=other.lease_id, key=MAIN)
    )
    assert loaded.entries == ()


async def test_finished_or_unknown_lease_is_a_conflict(
    slack: CoreClient, claude: CoreClient
) -> None:
    task = await _assignment(slack, claude, "1.0")
    query = TranscriptQuery(lease_id=uuid.uuid4(), key=MAIN)
    with pytest.raises(CoreConflictError):
        await claude.load_transcript(task.task_id, query)
    await claude.report_result(
        task.task_id, ResultReport(lease_id=task.lease_id, outcome=Succeeded(text="done"))
    )
    with pytest.raises(CoreConflictError):
        await claude.append_transcript(
            task.task_id, TranscriptAppend(lease_id=task.lease_id, key=MAIN, entries=ENTRIES)
        )


async def test_expired_lease_is_a_conflict(
    harness: Harness, slack: CoreClient, claude: CoreClient
) -> None:
    task = await _assignment(slack, claude, "1.0")
    harness.clock.advance(timedelta(minutes=5))
    with pytest.raises(CoreConflictError):
        await claude.load_transcript(
            task.task_id, TranscriptQuery(lease_id=task.lease_id, key=MAIN)
        )


async def test_other_engine_and_connector_are_rejected(
    harness: Harness, slack: CoreClient, claude: CoreClient
) -> None:
    task = await _assignment(slack, claude, "1.0")
    query = TranscriptQuery(lease_id=task.lease_id, key=MAIN)
    async with harness.client("gemini") as gemini:
        with pytest.raises(CoreNotFoundError):
            await gemini.load_transcript(task.task_id, query)
    with pytest.raises(CoreForbiddenError):
        await slack.load_transcript(task.task_id, query)


def test_append_needs_entries() -> None:
    with pytest.raises(ValueError, match="at least 1"):
        TranscriptAppend(lease_id=uuid.uuid4(), key=MAIN, entries=())
