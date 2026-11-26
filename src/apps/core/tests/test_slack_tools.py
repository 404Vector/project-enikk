"""Links, the custom emoji and scheduled messages for a session's thread (ADR-0042)."""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING

import pytest
from pydantic import HttpUrl

from enikk_contracts.common import ConnectorKind, ConversationRef
from enikk_contracts.events import InboundEvent
from enikk_contracts.outbox import OutboundKind, OutboxAck, OutboxPoll
from enikk_contracts.tasks import LeaseRequest
from enikk_contracts.thread_actions import (
    CancelScheduledRequest,
    EmojiQuery,
    LinkTarget,
    MessageLinkRequest,
    ScheduledQuery,
    ScheduleMessageRequest,
    ScheduleTarget,
    SlackWorkspaceReport,
    ThreadReactionRequest,
)
from enikk_core_client import CoreApiError, CoreNotFoundError
from enikk_core_domain.model import ReviewMessageRef

if TYPE_CHECKING:
    from conftest import Harness

    from enikk_contracts.tasks import TaskAssignment
    from enikk_core_client import CoreClient

pytestmark = pytest.mark.anyio

NOW = LeaseRequest(wait_seconds=0)
THREAD = ConversationRef(connector=ConnectorKind.SLACK, channel="D1", thread="1800.1")
POLL = OutboxPoll(wait_seconds=0)
WORKSPACE = SlackWorkspaceReport(url=HttpUrl("https://enikk.slack.com/"), emoji=("e-done",))


async def _leased(harness: Harness, claude: CoreClient, ts: str = "1800.1") -> TaskAssignment:
    async with harness.client("slack") as slack:
        await slack.submit_event(
            InboundEvent(
                event_id=f"slack:D1:{ts}",
                conversation=THREAD,
                user="U1",
                text="link please",
                agent="assistant",
            )
        )
    lease = (await claude.lease_task(NOW)).assignment
    assert lease is not None
    return lease


def _link(
    lease: TaskAssignment, target: LinkTarget, number: int | None = None
) -> MessageLinkRequest:
    return MessageLinkRequest(lease_id=lease.lease_id, target=target, number=number)


async def test_links_wait_for_the_workspace_report(harness: Harness, claude: CoreClient) -> None:
    lease = await _leased(harness, claude)
    with pytest.raises(CoreNotFoundError, match="not known yet"):
        await claude.message_link(lease.task_id, _link(lease, LinkTarget.THREAD))


async def test_links_to_the_thread_the_request_and_a_pull_request(
    harness: Harness, claude: CoreClient, slack: CoreClient
) -> None:
    await slack.report_slack_workspace(WORKSPACE)
    lease = await _leased(harness, claude, ts="1800.5")
    async with harness.db.unit_of_work() as uow:
        await uow.review_messages.put(
            ReviewMessageRef(
                repository="404Vector/project-enikk", number=121, channel="C0APPROVE", ts="9.1"
            )
        )
        await uow.commit()

    thread = await claude.message_link(lease.task_id, _link(lease, LinkTarget.THREAD))
    request = await claude.message_link(lease.task_id, _link(lease, LinkTarget.REQUEST))
    pull = await claude.message_link(lease.task_id, _link(lease, LinkTarget.PULL_REQUEST, 121))

    assert str(thread.url) == "https://enikk.slack.com/archives/D1/p18001"
    assert str(request.url) == (
        "https://enikk.slack.com/archives/D1/p18005?thread_ts=1800.1&cid=D1"
    )
    assert str(pull.url) == "https://enikk.slack.com/archives/C0APPROVE/p91"
    with pytest.raises(CoreNotFoundError):
        await claude.message_link(lease.task_id, _link(lease, LinkTarget.PULL_REQUEST, 9))
    with pytest.raises(CoreApiError):
        await claude.message_link(lease.task_id, _link(lease, LinkTarget.PULL_REQUEST))


async def test_the_emoji_list_and_a_note_for_an_unknown_reaction(
    harness: Harness, claude: CoreClient, slack: CoreClient
) -> None:
    lease = await _leased(harness, claude)
    before = await claude.list_emoji(lease.task_id, EmojiQuery(lease_id=lease.lease_id))
    assert not before.known

    await slack.report_slack_workspace(WORKSPACE)
    after = await claude.list_emoji(lease.task_id, EmojiQuery(lease_id=lease.lease_id))
    custom = await claude.react_in_thread(
        lease.task_id, ThreadReactionRequest(lease_id=lease.lease_id, name="e-done")
    )
    other = await claude.react_in_thread(
        lease.task_id, ThreadReactionRequest(lease_id=lease.lease_id, name="memo")
    )

    assert (after.known, after.names) == (True, ("e-done",))
    assert custom.note == ""
    assert "not a custom emoji" in other.note


async def test_only_the_slack_connector_reports_the_workspace(claude: CoreClient) -> None:
    with pytest.raises(CoreApiError):
        await claude.report_slack_workspace(WORKSPACE)


async def test_a_scheduled_message_waits_until_its_time(
    harness: Harness, claude: CoreClient, slack: CoreClient
) -> None:
    lease = await _leased(harness, claude)
    first = await slack.poll_outbox(POLL)
    await slack.ack_outbox(OutboxAck(message_ids=tuple(m.message_id for m in first.messages)))
    at = harness.clock.now() + timedelta(hours=2)

    thread = await claude.schedule_message(
        lease.task_id, ScheduleMessageRequest(lease_id=lease.lease_id, text="회의", at=at)
    )
    dm = await claude.schedule_message(
        lease.task_id,
        ScheduleMessageRequest(
            lease_id=lease.lease_id, text="약", at=at + timedelta(hours=1), target=ScheduleTarget.DM
        ),
    )

    assert not (await slack.poll_outbox(POLL)).messages
    listed = await claude.scheduled_messages(lease.task_id, ScheduledQuery(lease_id=lease.lease_id))
    assert [(m.message_id, m.target) for m in listed.messages] == [
        (thread.message_id, ScheduleTarget.THREAD),
        (dm.message_id, ScheduleTarget.DM),
    ]

    cancelled = await claude.cancel_scheduled(
        lease.task_id, CancelScheduledRequest(lease_id=lease.lease_id, message_id=dm.message_id)
    )
    assert cancelled.cancelled

    harness.clock.advance(timedelta(hours=2, minutes=1))
    due = (await slack.poll_outbox(POLL)).messages
    assert [(m.kind, m.text, m.conversation) for m in due] == [
        (OutboundKind.SCHEDULED, "회의", THREAD)
    ]
    await slack.ack_outbox(OutboxAck(message_ids=(due[0].message_id,)))
    harness.clock.advance(timedelta(hours=2))
    assert not (await slack.poll_outbox(POLL)).messages


async def test_a_schedule_must_be_in_the_future_and_within_120_days(
    harness: Harness, claude: CoreClient
) -> None:
    lease = await _leased(harness, claude)
    now = harness.clock.now()
    for at in (now - timedelta(minutes=1), now + timedelta(days=121)):
        with pytest.raises(CoreApiError):
            await claude.schedule_message(
                lease.task_id, ScheduleMessageRequest(lease_id=lease.lease_id, text="x", at=at)
            )


async def test_only_scheduled_messages_can_be_cancelled(
    harness: Harness, claude: CoreClient, slack: CoreClient
) -> None:
    lease = await _leased(harness, claude)
    [queued] = (await slack.poll_outbox(POLL)).messages
    with pytest.raises(CoreNotFoundError):
        await claude.cancel_scheduled(
            lease.task_id,
            CancelScheduledRequest(lease_id=lease.lease_id, message_id=queued.message_id),
        )
