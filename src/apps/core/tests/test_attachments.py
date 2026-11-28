"""The owner's attachments go from the connector to the leased task (ADR-0042 2b)."""

from __future__ import annotations

import base64
import uuid
from typing import TYPE_CHECKING

import pytest

from enikk_contracts.attachments import (
    AttachmentFetch,
    AttachmentInfo,
    AttachmentState,
    AttachmentUpload,
    TaskAttachment,
)
from enikk_contracts.common import ConnectorKind, ConversationRef
from enikk_contracts.events import InboundEvent
from enikk_contracts.tasks import LeaseRequest, ResultReport, Succeeded
from enikk_core_client import CoreApiError, CoreNotFoundError

if TYPE_CHECKING:
    from enikk_contracts.tasks import TaskAssignment
    from enikk_core_client import CoreClient

pytestmark = pytest.mark.anyio

NOW = LeaseRequest(wait_seconds=0)
THREAD = ConversationRef(connector=ConnectorKind.SLACK, channel="C1", thread="1900.1")
PNG = b"\x89PNG\r\n\x1a\n-image-"


def _event(*attachments: AttachmentInfo) -> InboundEvent:
    return InboundEvent(
        event_id="slack:C1:1900.1",
        conversation=THREAD,
        user="U1",
        text="look at this",
        agent="assistant",
        attachments=attachments,
    )


def _bytes(content: bytes) -> AttachmentUpload:
    return AttachmentUpload(content_base64=base64.b64encode(content).decode())


async def _leased(claude: CoreClient) -> TaskAssignment:
    lease = (await claude.lease_task(NOW)).assignment
    assert lease is not None
    return lease


async def test_the_lease_names_the_attachments_and_the_runtime_reads_them(
    claude: CoreClient, slack: CoreClient
) -> None:
    accepted = await slack.submit_event(
        _event(AttachmentInfo(name="a.png", size=len(PNG)), AttachmentInfo(name="b.zip", size=1))
    )
    lease = await _leased(claude)
    assert lease.attachments == (
        TaskAttachment(index=0, name="a.png", size=len(PNG)),
        TaskAttachment(index=1, name="b.zip", size=1),
    )
    fetch = AttachmentFetch(lease_id=lease.lease_id)

    pending = await claude.fetch_attachment(lease.task_id, 0, fetch)
    assert pending.state is AttachmentState.PENDING

    stored = await slack.upload_attachment(accepted.task_id, 0, _bytes(PNG))
    failed = await slack.upload_attachment(
        accepted.task_id, 1, AttachmentUpload(failure="Slack refused the download")
    )
    assert (stored.state, failed.state) == (AttachmentState.READY, AttachmentState.FAILED)

    ready = await claude.fetch_attachment(lease.task_id, 0, fetch)
    assert ready.content_base64 is not None
    assert base64.b64decode(ready.content_base64) == PNG
    refused = await claude.fetch_attachment(lease.task_id, 1, fetch)
    assert (refused.state, refused.detail) == (
        AttachmentState.FAILED,
        "Slack refused the download",
    )


async def test_a_settled_attachment_keeps_its_state(claude: CoreClient, slack: CoreClient) -> None:
    accepted = await slack.submit_event(_event(AttachmentInfo(name="a.png", size=len(PNG))))
    await slack.upload_attachment(accepted.task_id, 0, AttachmentUpload(failure="too large"))

    again = await slack.upload_attachment(accepted.task_id, 0, _bytes(PNG))

    assert again.state is AttachmentState.FAILED


async def test_the_attachments_are_dropped_when_the_task_ends(
    claude: CoreClient, slack: CoreClient
) -> None:
    accepted = await slack.submit_event(_event(AttachmentInfo(name="a.png", size=len(PNG))))
    await slack.upload_attachment(accepted.task_id, 0, _bytes(PNG))
    lease = await _leased(claude)

    await claude.report_result(
        lease.task_id, ResultReport(lease_id=lease.lease_id, outcome=Succeeded(text="seen"))
    )

    with pytest.raises(CoreNotFoundError):
        await slack.upload_attachment(accepted.task_id, 0, _bytes(PNG))


async def test_only_the_active_lease_reads_and_only_known_attachments_are_uploaded(
    claude: CoreClient, slack: CoreClient
) -> None:
    accepted = await slack.submit_event(_event(AttachmentInfo(name="a.png", size=len(PNG))))
    lease = await _leased(claude)

    with pytest.raises(CoreApiError):
        await claude.fetch_attachment(lease.task_id, 0, AttachmentFetch(lease_id=uuid.uuid4()))
    with pytest.raises(CoreNotFoundError):
        await slack.upload_attachment(accepted.task_id, 1, _bytes(PNG))
    with pytest.raises(CoreNotFoundError):
        await slack.upload_attachment(uuid.uuid4(), 0, _bytes(PNG))
