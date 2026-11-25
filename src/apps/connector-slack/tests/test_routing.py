from __future__ import annotations

from pathlib import Path

import pytest

from enikk_connector_slack.config import SlackConfig
from enikk_connector_slack.events import MessageRef, SlackMessage
from enikk_connector_slack.routing import Router
from enikk_connector_slack.testing import BOT, CONFIG, OWNER
from enikk_contracts.common import ConnectorKind
from enikk_kernel.errors import ConfigurationError

REPO = Path(__file__).resolve().parents[4]
router = Router(CONFIG, bot_user_id=BOT)


def _message(**fields: str) -> SlackMessage:
    values = {"type": "message", "channel": "C1", "user": OWNER, "text": "hi", "ts": "2.0"}
    values.update(fields)
    return SlackMessage.model_validate(values)


def test_direct_message_starts_a_conversation_in_its_own_thread() -> None:
    event = router.route(_message(channel="D1", channel_type="im"))
    assert event is not None
    assert event.event_id == "slack:D1:2.0"
    assert (event.conversation.connector, event.conversation.channel) == (ConnectorKind.SLACK, "D1")
    assert event.conversation.thread == "2.0"
    assert (event.agent, event.continue_only) == ("assistant", False)


def test_mention_routes_to_the_mention_agent_without_the_mention() -> None:
    event = router.route(_message(type="app_mention", text=f"<@{BOT}> summarize", thread_ts="1.0"))
    assert event is not None
    assert (event.text, event.agent, event.conversation.thread) == ("summarize", "helper", "1.0")
    assert not event.continue_only


def test_thread_reply_only_continues_an_existing_conversation() -> None:
    event = router.route(_message(channel_type="channel", thread_ts="1.0"))
    assert event is not None
    assert event.continue_only
    assert event.conversation.thread == "1.0"


@pytest.mark.parametrize(
    "fields",
    [
        {"user": "U0OTHER", "channel_type": "im"},  # not the owner (I-01)
        {"bot_id": "B1", "channel_type": "im"},  # bots, including itself
        {"subtype": "message_changed", "channel_type": "im"},
        {"channel_type": "channel"},  # channel message without mention or thread
        {"channel_type": "channel", "thread_ts": "1.0", "text": f"<@{BOT}> hi"},  # app_mention
        {"channel_type": "im", "text": f"  <@{BOT}>  "},  # nothing left to say
        {"type": "reaction_added"},
    ],
)
def test_ignored_messages(fields: dict[str, str]) -> None:
    assert router.route(_message(**fields)) is None


def test_mention_from_another_user_is_ignored() -> None:
    assert router.route(_message(type="app_mention", user="U0OTHER", text=f"<@{BOT}> hi")) is None


def test_message_ref_round_trips_through_the_event_id() -> None:
    ref = MessageRef(channel="C1", ts="1700000000.000100")
    assert MessageRef.parse(ref.event_id) == ref
    assert MessageRef.parse("smoke-123") is None
    assert MessageRef.parse("slack:C1") is None


def test_repository_config_is_valid_and_names_defined_agents() -> None:
    config = SlackConfig.load(REPO / "config" / "slack.yaml")
    assert config.reactions.replied == "e-done"
    assert config.owner == "U04CF12EKRA"
    for agent in (config.routes.direct_message, config.routes.mention):
        assert (REPO / "agents" / f"{agent}.yaml").is_file()


def test_invalid_config_is_a_configuration_error(tmp_path: Path) -> None:
    path = tmp_path / "slack.yaml"
    path.write_text("owner: someone\nroutes: {direct_message: assistant, mention: assistant}\n")
    with pytest.raises(ConfigurationError, match="invalid Slack config"):
        SlackConfig.load(path)
    with pytest.raises(ConfigurationError, match="cannot read"):
        SlackConfig.load(tmp_path / "missing.yaml")
