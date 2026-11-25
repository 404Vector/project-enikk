"""Custom emoji for the marks, with Slack's own as the fallback (ADR-0042)."""

from __future__ import annotations

import uuid

import pytest

from enikk_connector_slack.events import MessageRef
from enikk_connector_slack.marks import STANDARD, Mark, Marker, Reactions
from enikk_connector_slack.outbound import OutboxRelay, RelayPolicy
from enikk_connector_slack.testing import FakeCore, FakeSlack
from enikk_contracts.common import ConnectorKind, ConversationRef
from enikk_contracts.outbox import OutboundKind, OutboundMessage

pytestmark = pytest.mark.anyio

SOURCE = MessageRef(channel="D1", ts="5.0")
CUSTOM = Reactions(replied="e-done", remembered="e-remember", forgotten="e-forget")


async def test_a_custom_emoji_is_used_when_the_workspace_has_it() -> None:
    slack = FakeSlack()
    await Marker(slack, CUSTOM).mark(SOURCE, Mark.REPLIED)
    assert slack.reactions == [("+", "5.0", "e-done")]


async def test_an_unknown_custom_emoji_falls_back_to_slacks_own() -> None:
    slack = FakeSlack()
    slack.unknown_emoji.add("e-done")
    await Marker(slack, CUSTOM).mark(SOURCE, Mark.REPLIED)
    assert slack.reactions == [("+", "5.0", STANDARD[Mark.REPLIED])]


async def test_an_unset_mark_uses_slacks_own() -> None:
    slack = FakeSlack()
    await Marker(slack, CUSTOM).mark(SOURCE, Mark.RUNNING)
    assert slack.reactions == [("+", "5.0", "eyes")]


async def test_unmark_removes_both_names() -> None:
    slack = FakeSlack()
    await Marker(slack, CUSTOM).unmark(SOURCE, Mark.REPLIED)
    assert slack.reactions == [("-", "5.0", "e-done"), ("-", "5.0", "white_check_mark")]


def _memory(kind: OutboundKind) -> OutboundMessage:
    return OutboundMessage(
        message_id=uuid.uuid4(),
        task_id=uuid.uuid4(),
        source_event_id="slack:D1:5.0",
        conversation=ConversationRef(connector=ConnectorKind.SLACK, channel="D1", thread="5.0"),
        kind=kind,
        text="기억했습니다: 알림 위치 (enikk/선호/알림 위치.md)",
    )


@pytest.mark.parametrize(
    ("kind", "emoji"),
    [
        (OutboundKind.REMEMBERED, "e-remember"),
        (OutboundKind.FORGOTTEN, "e-forget"),
        (OutboundKind.MEMORY_UPDATED, STANDARD[Mark.MEMORY_UPDATED]),
    ],
)
async def test_a_memory_change_marks_the_message_without_a_post(
    kind: OutboundKind, emoji: str
) -> None:
    core, slack = FakeCore(), FakeSlack()
    core.pending = [_memory(kind)]
    relay = OutboxRelay(
        core,
        slack,
        RelayPolicy(wait_seconds=0, batch=10, backoff_max_seconds=4),
        marker=Marker(slack, CUSTOM),
    )

    assert await relay.relay_once() == 1

    assert slack.reactions == [("+", "5.0", emoji)]
    assert slack.posts == []
