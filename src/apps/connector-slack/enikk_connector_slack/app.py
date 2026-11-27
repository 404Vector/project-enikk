"""Composition root: fetches the Slack tokens from core, connects Socket Mode, relays the outbox."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
from typing import TYPE_CHECKING, Final

from slack_sdk.socket_mode.aiohttp import SocketModeClient
from slack_sdk.web.async_client import AsyncWebClient

from enikk_connector_slack.approvals import ApprovalActions, NoticeRelay
from enikk_connector_slack.config import SlackConfig
from enikk_connector_slack.inbound import InboundHandler
from enikk_connector_slack.marks import Marker
from enikk_connector_slack.outbound import OutboxRelay, RelayPolicy
from enikk_connector_slack.routing import Router
from enikk_connector_slack.slack import SlackWebApi, SocketListener
from enikk_connector_slack.workspace import WorkspaceReporter
from enikk_contracts.credentials import CredentialName, CredentialRequest
from enikk_core_client import CoreClient
from enikk_core_client.credentials import wait_for_credentials
from enikk_core_client.tokens import FileTokenSource
from enikk_kernel.errors import ConfigurationError

if TYPE_CHECKING:
    from pydantic import SecretStr

    from enikk_connector_slack.settings import ConnectorSlackSettings

SERVICE_NAME: Final = "enikk-connector-slack"
CREDENTIALS: Final = CredentialRequest(
    names=(CredentialName.SLACK_BOT_TOKEN, CredentialName.SLACK_APP_TOKEN)
)

_log = logging.getLogger(__name__)


class ConnectorSlackApp:
    def __init__(self, settings: ConnectorSlackSettings) -> None:
        self._settings = settings

    async def run(self) -> None:
        s = self._settings
        config = (
            SlackConfig.load(s.config_file)
            .with_deploy_channel(s.deploy_notify_channel)
            .with_approval_channel(s.approval_channel)
        )
        async with CoreClient.connect(s.core_url, FileTokenSource(s.token_file)) as core:
            values = (await wait_for_credentials(core, CREDENTIALS)).values
            bot_token = _value(values, CredentialName.SLACK_BOT_TOKEN)
            app_token = _value(values, CredentialName.SLACK_APP_TOKEN)
            web = AsyncWebClient(token=bot_token.get_secret_value())
            slack = SlackWebApi(web)
            router = Router(config, bot_user_id=await slack.bot_user_id())
            marker = Marker(slack, config.reactions)
            handler = InboundHandler(core, slack, router, s.submit_timeout_seconds, marker)
            relay = OutboxRelay(
                core,
                slack,
                RelayPolicy(
                    wait_seconds=s.outbox.wait_seconds,
                    batch=s.outbox.batch,
                    backoff_max_seconds=s.outbox.backoff_max_seconds,
                ),
                marker=marker,
                owner=config.owner,
                channels=config.agent_channels,
                running_emoji=config.running_emoji,
            )
            reporter = WorkspaceReporter(core, slack, s.workspace_report_seconds)
            socket = SocketModeClient(app_token=app_token.get_secret_value(), web_client=web)
            notices = NoticeRelay(
                core,
                slack,
                config,
                RelayPolicy(
                    wait_seconds=s.outbox.wait_seconds,
                    batch=s.outbox.batch,
                    backoff_max_seconds=s.outbox.backoff_max_seconds,
                ),
            )
            actions = ApprovalActions(core, slack, config)
            socket.socket_mode_request_listeners.append(SocketListener(handler, actions))

            stop = asyncio.Event()
            loop = asyncio.get_running_loop()
            for stop_signal in (signal.SIGTERM, signal.SIGINT):
                with contextlib.suppress(NotImplementedError):
                    loop.add_signal_handler(stop_signal, stop.set)

            # slack-sdk leaves connect/close unannotated.
            await socket.connect()  # type: ignore[no-untyped-call]
            relaying = asyncio.create_task(relay.run(stop))
            noticing = asyncio.create_task(notices.run(stop))
            reporting = asyncio.create_task(reporter.run(stop))
            stopping = asyncio.create_task(stop.wait())
            _log.info("%s ready: core %s, owner %s", SERVICE_NAME, s.core_url, config.owner)
            try:
                await asyncio.wait(
                    {relaying, noticing, stopping}, return_when=asyncio.FIRST_COMPLETED
                )
                for relay_task in (relaying, noticing):
                    if relay_task.done():
                        relay_task.result()  # re-raise why a relay ended
            finally:
                for task in (relaying, noticing, reporting, stopping):
                    task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await task
                await socket.close()  # type: ignore[no-untyped-call]


def _value(values: dict[CredentialName, SecretStr], name: CredentialName) -> SecretStr:
    value = values.get(name)
    if value is None:
        raise ConfigurationError(f"core did not return credential {name}")
    return value
