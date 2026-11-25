"""The owner's [취소] button on a running task (ADR-0037 3b)."""

from __future__ import annotations

import uuid

import pytest

from enikk_connector_slack import blocks
from enikk_connector_slack.approvals import ApprovalActions
from enikk_connector_slack.marks import STANDARD, Mark
from enikk_connector_slack.outbound import OutboxRelay, RelayPolicy
from enikk_connector_slack.testing import CONFIG, OWNER, FakeCore, FakeSlack
from enikk_contracts.common import ConnectorKind, ConversationRef
from enikk_contracts.outbox import OutboundKind, OutboundMessage
from enikk_core_client import CoreUnavailableError

pytestmark = pytest.mark.anyio

WAITING, RECEIVED = STANDARD[Mark.WAITING], STANDARD[Mark.RUNNING]
CANCELLED, REPLIED = STANDARD[Mark.CANCELLED], STANDARD[Mark.REPLIED]

POLICY = RelayPolicy(wait_seconds=0, batch=10, backoff_max_seconds=4)
TASK = uuid.uuid4()
THREAD = ConversationRef(connector=ConnectorKind.SLACK, channel="C1", thread="5.0")


def _message(kind: OutboundKind, text: str = "x") -> OutboundMessage:
    return OutboundMessage(
        message_id=uuid.uuid4(),
        task_id=TASK,
        source_event_id="slack:C1:5.0",
        conversation=THREAD,
        kind=kind,
        text=text,
    )


def _press(user: str, ts: str = "5.0.1") -> dict[str, object]:
    return {
        "type": "block_actions",
        "user": {"id": user},
        "container": {"type": "message", "channel_id": "C1", "message_ts": ts},
        "message": {"text": blocks.RUNNING_TEXT, "blocks": []},
        "actions": [{"action_id": blocks.CANCEL_ACTION, "value": str(TASK)}],
    }


async def _relay(core: FakeCore, slack: FakeSlack, *kinds: OutboundKind) -> None:
    core.pending.extend(_message(kind) for kind in kinds)
    await OutboxRelay(core, slack, POLICY).relay_once()


async def test_a_started_task_gets_one_line_with_the_button() -> None:
    core, slack = FakeCore(), FakeSlack()
    await _relay(core, slack, OutboundKind.STARTED, OutboundKind.STARTED)
    [(channel, thread, message)] = slack.replies
    assert (channel, thread) == ("C1", "5.0")
    [section] = message.payload()
    assert section["accessory"] == {
        "type": "button",
        "text": {"type": "plain_text", "text": "취소"},
        "action_id": blocks.CANCEL_ACTION,
        "value": str(TASK),
        "style": "danger",
    }
    assert slack.posts == []


@pytest.mark.parametrize("kind", [OutboundKind.REPLY, OutboundKind.FAILURE])
async def test_the_button_goes_away_when_the_task_ends(kind: OutboundKind) -> None:
    core, slack = FakeCore(), FakeSlack()
    relay = OutboxRelay(core, slack, POLICY)
    core.pending.append(_message(OutboundKind.STARTED))
    await relay.relay_once()
    core.pending.append(_message(kind))
    await relay.relay_once()
    assert slack.deleted == [("C1", "5.0.1")]


async def test_a_cancel_notice_is_posted_and_marked() -> None:
    core, slack = FakeCore(), FakeSlack()
    await _relay(core, slack, OutboundKind.CANCELLED)
    assert slack.posts == [("C1", "5.0", "x")]
    assert ("+", "5.0", CANCELLED) in slack.reactions


async def test_the_owner_cancels_and_the_button_is_removed() -> None:
    core, slack = FakeCore(), FakeSlack()
    await ApprovalActions(core, slack, CONFIG).handle(_press(OWNER))
    assert core.cancels == [TASK]
    assert slack.deleted == [("C1", "5.0.1")]
    assert slack.ephemerals == []


async def test_anyone_else_is_refused_privately() -> None:
    core, slack = FakeCore(), FakeSlack()
    await ApprovalActions(core, slack, CONFIG).handle(_press("U0OTHER"))
    assert core.cancels == []
    assert slack.deleted == []
    assert slack.ephemerals == [("C1", "U0OTHER", blocks.NOT_OWNER_TEXT)]


async def test_a_finished_task_tells_only_the_owner() -> None:
    core, slack = FakeCore(), FakeSlack()
    core.cancel_finds_nothing = True
    await ApprovalActions(core, slack, CONFIG).handle(_press(OWNER))
    assert slack.deleted == [("C1", "5.0.1")]
    assert slack.ephemerals == [("C1", OWNER, blocks.ALREADY_FINISHED_TEXT)]
    assert slack.posts == []


async def test_core_down_keeps_the_button_and_says_so_privately() -> None:
    core, slack = FakeCore(), FakeSlack()
    core.fail_with = CoreUnavailableError("down")
    await ApprovalActions(core, slack, CONFIG).handle(_press(OWNER))
    assert slack.deleted == []
    [(_, user, text)] = slack.ephemerals
    assert user == OWNER
    assert "취소하지 못했습니다" in text


async def test_the_request_waits_then_runs_then_shows_its_result() -> None:
    core, slack = FakeCore(), FakeSlack()
    relay = OutboxRelay(core, slack, POLICY)
    core.pending.append(_message(OutboundKind.STARTED))
    await relay.relay_once()
    core.pending.append(_message(OutboundKind.REPLY))
    await relay.relay_once()
    assert slack.reactions == [
        ("-", "5.0", WAITING),
        ("+", "5.0", RECEIVED),
        ("-", "5.0", WAITING),
        ("-", "5.0", RECEIVED),
        ("+", "5.0", REPLIED),
    ]
