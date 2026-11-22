"""Slack Web API and Socket Mode, through slack-sdk."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Final

import aiohttp
from pydantic import ValidationError
from slack_sdk.errors import SlackApiError
from slack_sdk.socket_mode.response import SocketModeResponse

from enikk_connector_slack.events import AuthIdentity, EventCallback
from enikk_connector_slack.ports import SlackDeliveryError
from enikk_kernel.errors import ConfigurationError

if TYPE_CHECKING:
    from slack_sdk.socket_mode.async_client import AsyncBaseSocketModeClient
    from slack_sdk.socket_mode.request import SocketModeRequest
    from slack_sdk.web.async_client import AsyncWebClient

    from enikk_connector_slack.events import MessageRef
    from enikk_connector_slack.inbound import InboundHandler

EVENTS_API: Final = "events_api"
MARKDOWN_LIMIT: Final = 12_000
"""Longest text ``chat.postMessage`` accepts as ``markdown_text``."""
TRANSIENT_ERRORS: Final = frozenset(
    {"ratelimited", "internal_error", "fatal_error", "service_unavailable", "request_timeout"}
)
IGNORED_REACTION_ERRORS: Final = frozenset({"already_reacted", "no_reaction"})

_log = logging.getLogger(__name__)


class SlackWebApi:
    def __init__(self, web: AsyncWebClient) -> None:
        self._web = web

    async def bot_user_id(self) -> str:
        try:
            response = await self._web.auth_test()
            return AuthIdentity.model_validate(response.data).user_id
        except (SlackApiError, ValidationError) as exc:
            raise ConfigurationError(f"Slack bot token check failed: {exc}") from exc

    async def post_reply(self, channel: str, thread: str, text: str) -> None:
        try:
            if len(text) <= MARKDOWN_LIMIT:
                await self._web.chat_postMessage(
                    channel=channel, thread_ts=thread, markdown_text=text
                )
            else:
                await self._web.chat_postMessage(channel=channel, thread_ts=thread, text=text)
        except SlackApiError as exc:
            error = _error_code(exc)
            status = exc.response.status_code
            transient = status == 429 or status >= 500 or error in TRANSIENT_ERRORS
            raise SlackDeliveryError(f"chat.postMessage: {error}", transient=transient) from exc
        except (aiohttp.ClientError, TimeoutError) as exc:
            raise SlackDeliveryError(f"chat.postMessage: {exc!r}", transient=True) from exc

    async def react(self, message: MessageRef, emoji: str) -> None:
        try:
            await self._web.reactions_add(channel=message.channel, timestamp=message.ts, name=emoji)
        except SlackApiError as exc:
            self._reaction_failed("add", emoji, _error_code(exc))
        except (aiohttp.ClientError, TimeoutError) as exc:
            self._reaction_failed("add", emoji, repr(exc))

    async def unreact(self, message: MessageRef, emoji: str) -> None:
        try:
            await self._web.reactions_remove(
                channel=message.channel, timestamp=message.ts, name=emoji
            )
        except SlackApiError as exc:
            self._reaction_failed("remove", emoji, _error_code(exc))
        except (aiohttp.ClientError, TimeoutError) as exc:
            self._reaction_failed("remove", emoji, repr(exc))

    @staticmethod
    def _reaction_failed(action: str, emoji: str, error: str) -> None:
        if error not in IGNORED_REACTION_ERRORS:
            _log.warning("could not %s reaction %s: %s", action, emoji, error)


class SocketListener:
    """Socket Mode request listener: settles each events_api envelope through the handler."""

    def __init__(self, handler: InboundHandler) -> None:
        self._handler = handler

    async def __call__(self, client: AsyncBaseSocketModeClient, request: SocketModeRequest) -> None:
        if request.type != EVENTS_API:
            await _acknowledge(client, request)
            return
        try:
            callback = EventCallback.model_validate(request.payload)
        except ValidationError:
            await _acknowledge(client, request)
            return
        decision = await self._handler.handle(callback)
        if decision.acknowledge:
            await _acknowledge(client, request)
            await self._handler.mark_received(decision)


async def _acknowledge(client: AsyncBaseSocketModeClient, request: SocketModeRequest) -> None:
    await client.send_socket_mode_response(SocketModeResponse(envelope_id=request.envelope_id))


def _error_code(exc: SlackApiError) -> str:
    error = exc.response.get("error")
    return error if isinstance(error, str) else "unknown_error"
