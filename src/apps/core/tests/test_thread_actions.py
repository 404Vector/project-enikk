"""A session posts files and reactions to the thread it answers (ADR-0042)."""

from __future__ import annotations

import base64
import uuid
from typing import TYPE_CHECKING

import pytest

from enikk_contracts.common import ConnectorKind, ConversationRef
from enikk_contracts.events import InboundEvent
from enikk_contracts.outbox import OutboundKind, OutboxAck, OutboxPoll
from enikk_contracts.tasks import LeaseRequest
from enikk_contracts.thread_actions import (
    MAX_FILES_PER_TASK,
    ThreadFileRequest,
    ThreadReactionRequest,
)
from enikk_core_client import CoreApiError, CoreNotFoundError

if TYPE_CHECKING:
    from conftest import Harness

    from enikk_contracts.tasks import TaskAssignment
    from enikk_core_client import CoreClient

pytestmark = pytest.mark.anyio

NOW = LeaseRequest(wait_seconds=0)
THREAD = ConversationRef(connector=ConnectorKind.SLACK, channel="C1", thread="1800.1")
POLL = OutboxPoll(wait_seconds=0)
PNG = b"\x89PNG\r\n\x1a\n-image-"


async def _leased(harness: Harness, claude: CoreClient) -> TaskAssignment:
    async with harness.client("slack") as slack:
        await slack.submit_event(
            InboundEvent(
                event_id="slack:C1:1800.1",
                conversation=THREAD,
                user="U1",
                text="send me the icons",
                agent="assistant",
            )
        )
    lease = (await claude.lease_task(NOW)).assignment
    assert lease is not None
    return lease


def _file(
    lease: TaskAssignment, content: bytes = PNG, name: str = "e-done.png"
) -> ThreadFileRequest:
    return ThreadFileRequest(
        lease_id=lease.lease_id,
        filename=name,
        title="done",
        content_base64=base64.b64encode(content).decode(),
    )


async def test_a_file_reaches_the_connector_and_is_dropped_once_delivered(
    harness: Harness, claude: CoreClient, slack: CoreClient
) -> None:
    lease = await _leased(harness, claude)

    await claude.post_thread_file(lease.task_id, _file(lease))

    batch = await slack.poll_outbox(POLL)
    sent = [m for m in batch.messages if m.kind is OutboundKind.FILE]
    assert len(sent) == 1
    assert (sent[0].text, sent[0].conversation, sent[0].source_event_id) == (
        "done",
        THREAD,
        "slack:C1:1800.1",
    )
    file = await slack.outbox_file(sent[0].message_id)
    assert (file.filename, base64.b64decode(file.content_base64)) == ("e-done.png", PNG)

    await slack.ack_outbox(OutboxAck(message_ids=tuple(m.message_id for m in batch.messages)))
    with pytest.raises(CoreNotFoundError):
        await slack.outbox_file(sent[0].message_id)


async def test_a_reaction_goes_to_the_owners_message(
    harness: Harness, claude: CoreClient, slack: CoreClient
) -> None:
    lease = await _leased(harness, claude)

    await claude.react_in_thread(
        lease.task_id, ThreadReactionRequest(lease_id=lease.lease_id, name="e-remember")
    )

    batch = await slack.poll_outbox(POLL)
    reactions = [m for m in batch.messages if m.kind is OutboundKind.REACTION]
    assert [(m.text, m.source_event_id) for m in reactions] == [("e-remember", "slack:C1:1800.1")]


async def test_a_text_file_with_a_secret_is_refused(harness: Harness, claude: CoreClient) -> None:
    lease = await _leased(harness, claude)
    secret = b"token: ghp_" + b"a" * 36

    with pytest.raises(CoreApiError):
        await claude.post_thread_file(lease.task_id, _file(lease, secret, "notes.txt"))


async def test_files_per_task_are_limited(harness: Harness, claude: CoreClient) -> None:
    lease = await _leased(harness, claude)
    for _ in range(MAX_FILES_PER_TASK):
        await claude.post_thread_file(lease.task_id, _file(lease))

    with pytest.raises(CoreApiError):
        await claude.post_thread_file(lease.task_id, _file(lease))


async def test_another_lease_cannot_post(harness: Harness, claude: CoreClient) -> None:
    lease = await _leased(harness, claude)
    stale = lease.model_copy(update={"lease_id": uuid.uuid4()})

    with pytest.raises(CoreApiError):
        await claude.post_thread_file(lease.task_id, _file(stale))
