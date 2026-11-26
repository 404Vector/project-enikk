"""Agent channels: routing and the agent's name and icon (ADR-0045)."""

from __future__ import annotations

import uuid
from pathlib import Path

import pydantic_core
import pytest
from pydantic import ValidationError

from enikk_connector_slack import blocks
from enikk_connector_slack.config import AgentChannel, Persona, Routes, SlackConfig
from enikk_connector_slack.events import SlackMessage
from enikk_connector_slack.outbound import OutboxRelay, RelayPolicy
from enikk_connector_slack.routing import Router
from enikk_connector_slack.slack import SlackWebApi
from enikk_connector_slack.testing import BOT, OWNER, FakeCore, FakeSlack
from enikk_contracts.common import ConnectorKind, ConversationRef
from enikk_contracts.outbox import OutboundKind, OutboundMessage

REPO = Path(__file__).resolve().parents[4]
RITA = Persona(name="리타", icon_emoji="e-rita")
CONFIG = SlackConfig(
    owner=OWNER,
    routes=Routes(direct_message="enikk", mention="enikk"),
    agent_channels=(AgentChannel(channel="C0RITA", agent="rita", persona=RITA),),
)
router = Router(CONFIG, bot_user_id=BOT)
POLICY = RelayPolicy(wait_seconds=0, batch=10, backoff_max_seconds=4)


def _message(**fields: str) -> SlackMessage:
    values = {
        "type": "message",
        "channel": "C0RITA",
        "channel_type": "channel",
        "user": OWNER,
        "text": "hi",
        "ts": "2.0",
    }
    values.update(fields)
    return SlackMessage.model_validate(values)


def test_top_level_message_starts_a_conversation_with_the_channels_agent() -> None:
    event = router.route(_message())
    assert event is not None
    assert (event.agent, event.continue_only, event.conversation.thread) == ("rita", False, "2.0")


def test_mention_in_the_channel_goes_to_its_agent_once() -> None:
    event = router.route(_message(text=f"<@{BOT}> draw"))
    assert event is not None
    assert (event.agent, event.text) == ("rita", "draw")
    assert router.route(_message(type="app_mention", text=f"<@{BOT}> draw")) is None


def test_thread_reply_in_the_channel_only_continues() -> None:
    event = router.route(_message(thread_ts="1.0"))
    assert event is not None
    assert (event.agent, event.continue_only, event.conversation.thread) == ("rita", True, "1.0")


def test_other_channels_keep_the_routes() -> None:
    assert router.route(_message(channel="C0OTHER")) is None
    event = router.route(_message(channel="C0OTHER", type="app_mention", text=f"<@{BOT}> hi"))
    assert event is not None
    assert event.agent == "enikk"


def test_only_the_owner_is_heard_in_an_agent_channel() -> None:
    assert router.route(_message(user="U0OTHER")) is None


def _config(**fields: object) -> SlackConfig:
    values = {"owner": OWNER, "routes": {"direct_message": "enikk", "mention": "enikk"}, **fields}
    return SlackConfig.model_validate_json(pydantic_core.to_json(values))


def test_an_agent_channel_appears_once_and_is_not_a_notice_channel() -> None:
    rita = {"channel": "C0RITA", "agent": "rita", "persona": {"name": "리타"}}
    with pytest.raises(ValidationError, match="only once"):
        _config(agent_channels=[rita, {**rita, "agent": "other"}])
    with pytest.raises(ValidationError, match="notice channel"):
        _config(approval_channel="C0RITA", agent_channels=[rita])


def test_repository_agent_channels_name_defined_agents() -> None:
    config = SlackConfig.load(REPO / "config" / "slack.yaml")
    assert config.agent_channels
    for channel in config.agent_channels:
        assert (REPO / "agents" / f"{channel.agent}.yaml").is_file()


def _outbound(channel: str, kind: OutboundKind = OutboundKind.REPLY) -> OutboundMessage:
    return OutboundMessage(
        message_id=uuid.uuid4(),
        task_id=uuid.uuid4(),
        source_event_id=f"slack:{channel}:5.0",
        conversation=ConversationRef(connector=ConnectorKind.SLACK, channel=channel, thread="5.0"),
        kind=kind,
        text="answer",
    )


@pytest.mark.anyio
async def test_replies_in_an_agent_channel_show_its_persona() -> None:
    core, slack = FakeCore(), FakeSlack()
    core.pending = [_outbound("C0RITA"), _outbound("C0RITA", OutboundKind.FAILURE)]
    core.pending.append(_outbound("D1"))
    relay = OutboxRelay(core, slack, POLICY, channels=CONFIG.agent_channels)
    assert await relay.relay_once() == 3
    assert [p[0] for p in slack.posts] == ["C0RITA", "C0RITA", "D1"]
    assert slack.personas == [("C0RITA", RITA), ("C0RITA", RITA)]


@pytest.mark.anyio
async def test_the_cancel_button_in_an_agent_channel_shows_its_persona() -> None:
    core, slack = FakeCore(), FakeSlack()
    core.pending = [
        _outbound("C0RITA", OutboundKind.STARTED),
        _outbound("D1", OutboundKind.STARTED),
    ]
    relay = OutboxRelay(core, slack, POLICY, channels=CONFIG.agent_channels)
    assert await relay.relay_once() == 2
    assert [r[0] for r in slack.replies] == ["C0RITA", "D1"]
    assert slack.personas == [("C0RITA", RITA)]


class _Web:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def chat_postMessage(self, **kwargs: object) -> dict[str, object]:  # noqa: N802
        self.calls.append(kwargs)
        return {"ok": True, "channel": "C0RITA", "ts": "6.0"}


@pytest.mark.anyio
async def test_persona_sets_the_name_and_icon_of_the_post() -> None:
    web = _Web()
    api = SlackWebApi(web)  # type: ignore[arg-type]
    await api.post_reply("C0RITA", "5.0", "hi", RITA)
    await api.post_reply("C0RITA", "5.0", "hi", Persona(name="리타"))
    await api.post_reply("D1", None, "hi")
    assert web.calls[0]["username"] == "리타"
    assert web.calls[0]["icon_emoji"] == ":e-rita:"
    assert web.calls[1]["icon_emoji"] is None
    assert (web.calls[2]["username"], web.calls[2]["icon_emoji"]) == (None, None)
    await api.post_message("C0RITA", blocks.running("t-1"), thread="5.0", persona=RITA)
    assert (web.calls[3]["username"], web.calls[3]["icon_emoji"]) == ("리타", ":e-rita:")
