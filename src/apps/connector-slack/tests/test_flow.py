from __future__ import annotations

import asyncio
import uuid

import pytest

from enikk_connector_slack.events import EventCallback, SlackMessage
from enikk_connector_slack.inbound import InboundHandler
from enikk_connector_slack.outbound import FAILURE_PREFIX, OutboxRelay, RelayPolicy
from enikk_connector_slack.ports import SlackDeliveryError
from enikk_connector_slack.routing import Router
from enikk_connector_slack.testing import BOT, CONFIG, OWNER, FakeCore, FakeSlack
from enikk_contracts.common import ConnectorKind, ConversationRef
from enikk_contracts.outbox import OutboundKind, OutboundMessage
from enikk_core_client import CoreInvalidRequestError, CoreUnavailableError

pytestmark = pytest.mark.anyio

POLICY = RelayPolicy(wait_seconds=0, batch=10, backoff_max_seconds=4)


def _callback(**fields: str) -> EventCallback:
    values = {
        "type": "message",
        "channel": "D1",
        "channel_type": "im",
        "user": OWNER,
        "text": "hello",
        "ts": "5.0",
    }
    values.update(fields)
    return EventCallback(type="event_callback", event=SlackMessage.model_validate(values))


def _handler(core: FakeCore, slack: FakeSlack) -> InboundHandler:
    return InboundHandler(core, slack, Router(CONFIG, bot_user_id=BOT), submit_timeout=1)


def _outbound(
    kind: OutboundKind = OutboundKind.REPLY, source: str = "slack:D1:5.0"
) -> OutboundMessage:
    return OutboundMessage(
        message_id=uuid.uuid4(),
        task_id=uuid.uuid4(),
        source_event_id=source,
        conversation=ConversationRef(connector=ConnectorKind.SLACK, channel="D1", thread="5.0"),
        kind=kind,
        text="**answer**",
    )


async def test_accepted_message_is_acknowledged_then_marked() -> None:
    core, slack = FakeCore(), FakeSlack()
    handler = _handler(core, slack)
    decision = await handler.handle(_callback())
    assert decision.acknowledge
    assert [e.event_id for e in core.events] == ["slack:D1:5.0"]
    await handler.mark_received(decision)
    assert slack.reactions == [("+", "5.0", "eyes")]


async def test_redelivered_message_is_not_marked_again() -> None:
    core, slack = FakeCore(), FakeSlack()
    handler = _handler(core, slack)
    await handler.handle(_callback())
    again = await handler.handle(_callback())
    assert again.acknowledge
    assert again.received is None
    assert len(core.events) == 1


async def test_core_outage_leaves_the_event_for_slack_to_redeliver() -> None:
    core, slack = FakeCore(), FakeSlack()
    core.fail_with = CoreUnavailableError("down")
    assert not (await _handler(core, slack).handle(_callback())).acknowledge


async def test_rejected_event_is_acknowledged_without_a_mark() -> None:
    core, slack = FakeCore(), FakeSlack()
    core.fail_with = CoreInvalidRequestError("unknown agent")
    decision = await _handler(core, slack).handle(_callback())
    assert decision.acknowledge
    assert decision.received is None


async def test_thread_reply_without_a_conversation_is_dropped() -> None:
    core, slack = FakeCore(), FakeSlack()
    handler = _handler(core, slack)
    reply = _callback(channel="C1", channel_type="channel", ts="6.0", thread_ts="1.0")
    assert (await handler.handle(reply)).received is None
    assert core.events == []

    await handler.handle(_callback(type="app_mention", channel="C1", thread_ts="1.0", ts="5.5"))
    assert (await handler.handle(reply)).received is not None
    assert [e.continue_only for e in core.events] == [False, True]


async def test_relay_posts_in_the_thread_swaps_the_mark_and_acknowledges() -> None:
    core, slack = FakeCore(), FakeSlack()
    reply, failure = _outbound(), _outbound(OutboundKind.FAILURE, source="smoke-1")
    core.pending = [reply, failure]
    assert await OutboxRelay(core, slack, POLICY).relay_once() == 2
    assert slack.posts == [
        ("D1", "5.0", "**answer**"),
        ("D1", "5.0", FAILURE_PREFIX + "**answer**"),
    ]
    assert slack.reactions == [("-", "5.0", "eyes"), ("+", "5.0", "white_check_mark")]
    assert core.acked == [reply.message_id, failure.message_id]


async def test_relay_marks_failures() -> None:
    core, slack = FakeCore(), FakeSlack()
    core.pending = [_outbound(OutboundKind.FAILURE)]
    await OutboxRelay(core, slack, POLICY).relay_once()
    assert slack.reactions[-1] == ("+", "5.0", "x")


async def test_transient_post_failure_keeps_the_rest_pending() -> None:
    core, slack = FakeCore(), FakeSlack()
    first, second = _outbound(), _outbound()
    core.pending = [first, second]
    slack.fail_posts = [
        SlackDeliveryError("channel_not_found", transient=False),
        SlackDeliveryError("ratelimited", transient=True),
    ]
    with pytest.raises(SlackDeliveryError):
        await OutboxRelay(core, slack, POLICY).relay_once()
    # The permanently failing message is dropped and marked; the transient one stays.
    assert core.acked == [first.message_id]
    assert core.pending == [second]
    assert slack.reactions == [("-", "5.0", "eyes"), ("+", "5.0", "x")]


async def test_relay_backs_off_until_stopped() -> None:
    core, slack = FakeCore(), FakeSlack()
    stop = asyncio.Event()
    pauses: list[float] = []

    async def poll_down(*_: object) -> None:
        raise CoreUnavailableError("down")

    async def pause(seconds: float) -> None:
        pauses.append(seconds)
        if len(pauses) == 4:
            stop.set()

    core.poll_outbox = poll_down  # type: ignore[method-assign,assignment]
    await OutboxRelay(core, slack, POLICY, sleep=pause).run(stop)
    assert pauses == [1.0, 2.0, 4.0, 4.0]
