from __future__ import annotations

from typing import TYPE_CHECKING, cast

import pytest
from slack_sdk.socket_mode.request import SocketModeRequest

from enikk_connector_slack.inbound import InboundHandler
from enikk_connector_slack.routing import Router
from enikk_connector_slack.settings import ConnectorSlackSettings
from enikk_connector_slack.slack import SocketListener
from enikk_connector_slack.testing import BOT, CONFIG, OWNER, FakeCore, FakeSlack
from enikk_core_client import CoreUnavailableError

if TYPE_CHECKING:
    from slack_sdk.socket_mode.async_client import AsyncBaseSocketModeClient
    from slack_sdk.socket_mode.response import SocketModeResponse

pytestmark = pytest.mark.anyio


class FakeSocket:
    def __init__(self) -> None:
        self.acknowledged: list[str] = []

    async def send_socket_mode_response(self, response: SocketModeResponse) -> None:
        self.acknowledged.append(response.envelope_id)


def _request(envelope: str, request_type: str = "events_api") -> SocketModeRequest:
    payload = {
        "type": "event_callback",
        "event_id": "Ev1",
        "event": {
            "type": "message",
            "channel": "D1",
            "channel_type": "im",
            "user": OWNER,
            "text": "hello",
            "ts": "5.0",
            "client_msg_id": "ignored",
        },
    }
    return SocketModeRequest(type=request_type, envelope_id=envelope, payload=payload)


def _listener(core: FakeCore, slack: FakeSlack) -> SocketListener:
    return SocketListener(InboundHandler(core, slack, Router(CONFIG, BOT), submit_timeout=1))


async def test_listener_acknowledges_then_marks() -> None:
    core, slack, socket = FakeCore(), FakeSlack(), FakeSocket()
    client = cast("AsyncBaseSocketModeClient", socket)
    await _listener(core, slack)(client, _request("env-1"))
    assert socket.acknowledged == ["env-1"]
    assert slack.reactions == [("+", "5.0", "hourglass_flowing_sand")]


async def test_listener_withholds_the_acknowledgement_while_core_is_down() -> None:
    core, slack, socket = FakeCore(), FakeSlack(), FakeSocket()
    core.fail_with = CoreUnavailableError("down")
    await _listener(core, slack)(cast("AsyncBaseSocketModeClient", socket), _request("env-1"))
    assert socket.acknowledged == []


async def test_other_envelopes_are_acknowledged_and_ignored() -> None:
    core, slack, socket = FakeCore(), FakeSlack(), FakeSocket()
    client = cast("AsyncBaseSocketModeClient", socket)
    await _listener(core, slack)(client, _request("env-2", "interactive"))
    assert socket.acknowledged == ["env-2"]
    assert core.events == []


def test_defaults_point_at_the_cluster_and_the_bundled_config() -> None:
    settings = ConnectorSlackSettings()
    assert settings.core_url == "http://enikk-core.enikk.svc"
    assert str(settings.config_file) == "/app/config/slack.yaml"
    assert settings.submit_timeout_seconds < 3
