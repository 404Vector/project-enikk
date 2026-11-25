"""Files and reactions a session sends to its thread, and attached files (ADR-0042)."""

from __future__ import annotations

import base64
import uuid

import pytest

from enikk_connector_slack.events import SlackMessage
from enikk_connector_slack.outbound import FILE_FAILED, OutboxRelay, RelayPolicy
from enikk_connector_slack.ports import SlackDeliveryError
from enikk_connector_slack.routing import Router
from enikk_connector_slack.testing import BOT, CONFIG, OWNER, FakeCore, FakeSlack
from enikk_contracts.common import ConnectorKind, ConversationRef
from enikk_contracts.outbox import OutboundKind, OutboundMessage, OutboxFile

pytestmark = pytest.mark.anyio

POLICY = RelayPolicy(wait_seconds=0, batch=10, backoff_max_seconds=4)
PNG = b"\x89PNG-image"


def _outbound(kind: OutboundKind, text: str) -> OutboundMessage:
    return OutboundMessage(
        message_id=uuid.uuid4(),
        task_id=uuid.uuid4(),
        source_event_id="slack:D1:5.0",
        conversation=ConversationRef(connector=ConnectorKind.SLACK, channel="D1", thread="5.0"),
        kind=kind,
        text=text,
    )


def _with_file(core: FakeCore, message: OutboundMessage) -> None:
    core.files[message.message_id] = OutboxFile(
        filename="e-done.png", content_base64=base64.b64encode(PNG).decode()
    )


async def test_a_file_is_uploaded_in_the_thread_without_marking_the_request() -> None:
    core, slack = FakeCore(), FakeSlack()
    message = _outbound(OutboundKind.FILE, "done icon")
    _with_file(core, message)
    core.pending = [message]

    assert await OutboxRelay(core, slack, POLICY).relay_once() == 1

    assert slack.files == [("D1", "5.0", "e-done.png", PNG, "done icon")]
    assert slack.reactions == []
    assert slack.posts == []
    assert core.acked == [message.message_id]


async def test_a_refused_file_is_reported_in_the_thread() -> None:
    core, slack = FakeCore(), FakeSlack()
    message = _outbound(OutboundKind.FILE, "done icon")
    _with_file(core, message)
    core.pending = [message]
    slack.fail_files.append(SlackDeliveryError("files.uploadV2: invalid", transient=False))

    await OutboxRelay(core, slack, POLICY).relay_once()

    assert slack.posts == [("D1", "5.0", FILE_FAILED.format(name="e-done.png"))]
    assert core.acked == [message.message_id]


async def test_a_reaction_goes_on_the_source_message_only() -> None:
    core, slack = FakeCore(), FakeSlack()
    message = _outbound(OutboundKind.REACTION, "e-remember")
    core.pending = [message]

    await OutboxRelay(core, slack, POLICY).relay_once()

    assert slack.reactions == [("+", "5.0", "e-remember")]
    assert slack.posts == []


def _message(**fields: object) -> SlackMessage:
    values: dict[str, object] = {
        "type": "message",
        "channel": "D1",
        "channel_type": "im",
        "user": OWNER,
        "text": "look",
        "ts": "2.0",
    }
    values.update(fields)
    return SlackMessage.model_validate(values)


def test_a_message_with_files_is_routed_and_names_them() -> None:
    router = Router(CONFIG, bot_user_id=BOT)
    event = router.route(_message(subtype="file_share", files=[{"name": "a.png"}]))
    assert event is not None
    assert event.text.startswith("look\n\n[첨부 파일 1개: a.png.")


def test_files_without_text_still_reach_the_agent() -> None:
    router = Router(CONFIG, bot_user_id=BOT)
    event = router.route(_message(subtype="file_share", text="", files=[{"name": "a.pdf"}]))
    assert event is not None
    assert event.text.startswith("[첨부 파일 1개: a.pdf.")


def test_other_subtypes_are_still_ignored() -> None:
    router = Router(CONFIG, bot_user_id=BOT)
    assert router.route(_message(subtype="message_changed")) is None
