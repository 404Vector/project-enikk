"""The workspace report and scheduled messages (ADR-0042 stage 2)."""

from __future__ import annotations

import uuid

import pytest

from enikk_connector_slack.outbound import OutboxRelay, RelayPolicy
from enikk_connector_slack.ports import SlackDeliveryError, WorkspaceView
from enikk_connector_slack.testing import OWNER, FakeCore, FakeSlack
from enikk_connector_slack.workspace import WorkspaceReporter
from enikk_contracts.common import ConnectorKind, ConversationRef
from enikk_contracts.outbox import OutboundKind, OutboundMessage

pytestmark = pytest.mark.anyio

POLICY = RelayPolicy(wait_seconds=0, batch=10, backoff_max_seconds=4)


def _scheduled(kind: OutboundKind) -> OutboundMessage:
    return OutboundMessage(
        message_id=uuid.uuid4(),
        task_id=uuid.uuid4(),
        source_event_id="slack:D1:5.0",
        conversation=ConversationRef(connector=ConnectorKind.SLACK, channel="D1", thread="5.0"),
        kind=kind,
        text="회의 10분 전입니다.",
    )


async def test_the_workspace_address_and_emoji_reach_core() -> None:
    core, slack = FakeCore(), FakeSlack()

    assert await WorkspaceReporter(core, slack, 600).report_once()

    [report] = core.workspace_reports
    assert (str(report.url), report.emoji) == ("https://enikk.slack.test/", ("e-done",))


async def test_without_emoji_read_the_emoji_stay_unknown() -> None:
    core, slack = FakeCore(), FakeSlack()
    slack.view = WorkspaceView(url="https://enikk.slack.test/", emoji=None)

    assert await WorkspaceReporter(core, slack, 600).report_once()

    assert core.workspace_reports[0].emoji is None


async def test_a_failed_report_waits_for_the_next_round() -> None:
    core, slack = FakeCore(), FakeSlack()
    slack.fail_workspace.append(SlackDeliveryError("auth.test: down", transient=True))

    assert not await WorkspaceReporter(core, slack, 600).report_once()
    assert core.workspace_reports == []


async def test_a_scheduled_message_is_posted_in_its_thread_without_marks() -> None:
    core, slack = FakeCore(), FakeSlack()
    message = _scheduled(OutboundKind.SCHEDULED)
    core.pending = [message]

    assert await OutboxRelay(core, slack, POLICY, owner=OWNER).relay_once() == 1

    assert slack.posts == [("D1", "5.0", "회의 10분 전입니다.")]
    assert slack.reactions == []
    assert core.acked == [message.message_id]


async def test_a_scheduled_dm_is_a_new_message_to_the_owner() -> None:
    core, slack = FakeCore(), FakeSlack()
    core.pending = [_scheduled(OutboundKind.SCHEDULED_DM)]

    await OutboxRelay(core, slack, POLICY, owner=OWNER).relay_once()

    assert slack.posts == [(OWNER, None, "회의 10분 전입니다.")]
    assert slack.reactions == []
