"""Slack Web API and Socket Mode, through slack-sdk."""

from __future__ import annotations

import asyncio
import logging
import re
from typing import TYPE_CHECKING, Final

import aiohttp
from pydantic import ValidationError
from slack_sdk.errors import SlackApiError
from slack_sdk.socket_mode.response import SocketModeResponse

from enikk_connector_slack.events import AuthIdentity, EventCallback, PostedMessage, SlackFile
from enikk_connector_slack.ports import SlackDeliveryError, WorkspaceView
from enikk_kernel.errors import ConfigurationError

if TYPE_CHECKING:
    from pydantic import JsonValue
    from slack_sdk.socket_mode.async_client import AsyncBaseSocketModeClient
    from slack_sdk.socket_mode.request import SocketModeRequest
    from slack_sdk.web.async_client import AsyncWebClient

    from enikk_connector_slack.approvals import ApprovalActions
    from enikk_connector_slack.blocks import Message
    from enikk_connector_slack.config import Persona
    from enikk_connector_slack.events import MessageRef
    from enikk_connector_slack.inbound import InboundHandler

EVENTS_API: Final = "events_api"
INTERACTIVE: Final = "interactive"
MARKDOWN_LIMIT: Final = 12_000
"""Longest text ``chat.postMessage`` accepts as ``markdown_text``."""
TRANSIENT_ERRORS: Final = frozenset(
    {"ratelimited", "internal_error", "fatal_error", "service_unavailable", "request_timeout"}
)
IGNORED_REACTION_ERRORS: Final = frozenset({"already_reacted", "no_reaction"})
UNKNOWN_EMOJI: Final = "invalid_name"
EMOJI_NAME: Final = re.compile(r"[a-z0-9_+'\-]{1,100}")

SLACK_FILES: Final = "https://files.slack.com/"
"""Attachments are downloaded only from here, so the bot token goes nowhere else."""
DOWNLOAD_TIMEOUT: Final = aiohttp.ClientTimeout(total=120)
HTTP_OK: Final = 200

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

    async def workspace(self) -> WorkspaceView:
        """The workspace's address and custom emoji names (``None`` without ``emoji:read``).

        Raises ``SlackDeliveryError`` when the address cannot be read."""
        try:
            identity = await self._web.auth_test()
        except SlackApiError as exc:
            raise _delivery_error("auth.test", exc) from exc
        except (aiohttp.ClientError, TimeoutError) as exc:
            raise SlackDeliveryError(f"auth.test: {exc!r}", transient=True) from exc
        url = identity.get("url")
        if not isinstance(url, str) or not url:
            raise SlackDeliveryError("auth.test gave no workspace url", transient=True)
        return WorkspaceView(url=url, emoji=await self._custom_emoji())

    async def _custom_emoji(self) -> tuple[str, ...] | None:
        try:
            response = await self._web.emoji_list()
        except SlackApiError as exc:
            _log.warning("could not list the custom emoji: %s", _error_code(exc))
            return None
        except (aiohttp.ClientError, TimeoutError) as exc:
            _log.warning("could not list the custom emoji: %r", exc)
            return None
        found = response.get("emoji")
        if not isinstance(found, dict):
            return None
        return tuple(sorted(name for name in found if EMOJI_NAME.fullmatch(name)))

    async def post_reply(
        self, channel: str, thread: str | None, text: str, persona: Persona | None = None
    ) -> None:
        username, icon = _shown_as(persona)
        try:
            if len(text) <= MARKDOWN_LIMIT:
                await self._web.chat_postMessage(
                    channel=channel,
                    thread_ts=thread,
                    markdown_text=text,
                    username=username,
                    icon_emoji=icon,
                )
            else:
                await self._web.chat_postMessage(
                    channel=channel, thread_ts=thread, text=text, username=username, icon_emoji=icon
                )
        except SlackApiError as exc:
            raise _delivery_error("chat.postMessage", exc) from exc
        except (aiohttp.ClientError, TimeoutError) as exc:
            raise SlackDeliveryError(f"chat.postMessage: {exc!r}", transient=True) from exc

    async def post_message(
        self,
        channel: str,
        message: Message,
        thread: str | None = None,
        persona: Persona | None = None,
    ) -> PostedMessage:
        username, icon = _shown_as(persona)
        try:
            response = await self._web.chat_postMessage(
                channel=channel,
                thread_ts=thread,
                text=message.text,
                blocks=message.payload(),
                username=username,
                icon_emoji=icon,
            )
            return PostedMessage.model_validate(
                {"channel": response.get("channel"), "ts": response.get("ts")}
            )
        except SlackApiError as exc:
            raise _delivery_error("chat.postMessage", exc) from exc
        except (aiohttp.ClientError, TimeoutError) as exc:
            raise SlackDeliveryError(f"chat.postMessage: {exc!r}", transient=True) from exc
        except ValidationError as exc:
            raise SlackDeliveryError("chat.postMessage: no channel/ts", transient=False) from exc

    async def download_file(self, file: SlackFile, limit: int) -> bytes:
        url = file.url_private_download
        if url is None and file.id:
            try:
                info = await self._web.files_info(file=file.id)
            except SlackApiError as exc:
                raise _delivery_error("files.info", exc) from exc
            found = info.get("file")
            url = found.get("url_private_download") if isinstance(found, dict) else None
        if not isinstance(url, str) or not url.startswith(SLACK_FILES):
            raise SlackDeliveryError(f"file {file.id}: no Slack download address", transient=False)
        headers = {"Authorization": f"Bearer {self._web.token}"}
        try:
            async with (
                aiohttp.ClientSession(timeout=DOWNLOAD_TIMEOUT) as session,
                session.get(url, headers=headers, allow_redirects=False) as response,
            ):
                if response.status != HTTP_OK:
                    raise SlackDeliveryError(
                        f"file {file.id}: download answered {response.status}", transient=False
                    )
                content = await response.content.read(limit + 1)
        except (aiohttp.ClientError, TimeoutError) as exc:
            raise SlackDeliveryError(f"file {file.id}: {exc!r}", transient=True) from exc
        if len(content) > limit:
            raise SlackDeliveryError(f"larger than {limit} bytes", transient=False)
        return content

    async def upload_text(
        self, channel: str, thread: str, filename: str, content: str, title: str
    ) -> None:
        try:
            await self._web.files_upload_v2(
                channel=channel, thread_ts=thread, filename=filename, content=content, title=title
            )
        except SlackApiError as exc:
            _log.warning("could not attach %s: %s", filename, _error_code(exc))
        except (aiohttp.ClientError, TimeoutError) as exc:
            _log.warning("could not attach %s: %r", filename, exc)

    async def upload_file(
        self,
        channel: str,
        thread: str,
        filename: str,
        content: bytes,
        title: str,
        persona: Persona | None = None,
    ) -> None:
        username, icon = _shown_as(persona)
        try:
            await self._web.files_upload_v2(
                channel=channel,
                thread_ts=thread,
                filename=filename,
                content=content,
                title=title,
                username=username,
                icon_emoji=icon,
            )
        except SlackApiError as exc:
            raise _delivery_error("files.uploadV2", exc) from exc
        except (aiohttp.ClientError, TimeoutError) as exc:
            raise SlackDeliveryError(f"files.uploadV2: {exc!r}", transient=True) from exc

    async def update_message(
        self, channel: str, ts: str, text: str, blocks: list[dict[str, JsonValue]]
    ) -> None:
        try:
            await self._web.chat_update(channel=channel, ts=ts, text=text, blocks=blocks)
        except SlackApiError as exc:
            _log.warning("could not update message %s: %s", ts, _error_code(exc))
        except (aiohttp.ClientError, TimeoutError) as exc:
            _log.warning("could not update message %s: %r", ts, exc)

    async def delete_message(self, channel: str, ts: str) -> None:
        try:
            await self._web.chat_delete(channel=channel, ts=ts)
        except SlackApiError as exc:
            if _error_code(exc) != "message_not_found":
                _log.warning("could not delete message %s: %s", ts, _error_code(exc))
        except (aiohttp.ClientError, TimeoutError) as exc:
            _log.warning("could not delete message %s: %r", ts, exc)

    async def post_ephemeral(self, channel: str, user: str, text: str) -> None:
        try:
            await self._web.chat_postEphemeral(channel=channel, user=user, text=text)
        except SlackApiError as exc:
            _log.warning("could not tell %s privately: %s", user, _error_code(exc))
        except (aiohttp.ClientError, TimeoutError) as exc:
            _log.warning("could not tell %s privately: %r", user, exc)

    async def permalink(self, channel: str, ts: str) -> str | None:
        try:
            response = await self._web.chat_getPermalink(channel=channel, message_ts=ts)
        except SlackApiError as exc:
            _log.warning("no permalink for %s: %s", ts, _error_code(exc))
            return None
        except (aiohttp.ClientError, TimeoutError) as exc:
            _log.warning("no permalink for %s: %r", ts, exc)
            return None
        link = response.get("permalink")
        return link if isinstance(link, str) and link else None

    async def channel_id(self, name: str) -> str | None:
        cursor: str | None = None
        try:
            while True:
                response = await self._web.conversations_list(
                    types="public_channel,private_channel",
                    exclude_archived=True,
                    limit=200,
                    cursor=cursor,
                )
                for channel in response.get("channels") or []:
                    if isinstance(channel, dict) and channel.get("name") == name:
                        found = channel.get("id")
                        return found if isinstance(found, str) else None
                meta = response.get("response_metadata") or {}
                cursor = meta.get("next_cursor") if isinstance(meta, dict) else None
                if not cursor:
                    return None
        except SlackApiError as exc:
            _log.warning("could not look up channel %s: %s", name, _error_code(exc))
        except (aiohttp.ClientError, TimeoutError) as exc:
            _log.warning("could not look up channel %s: %r", name, exc)
        return None

    async def react(self, message: MessageRef, emoji: str) -> bool:
        try:
            await self._web.reactions_add(channel=message.channel, timestamp=message.ts, name=emoji)
        except SlackApiError as exc:
            error = _error_code(exc)
            self._reaction_failed("add", emoji, error)
            return error != UNKNOWN_EMOJI
        except (aiohttp.ClientError, TimeoutError) as exc:
            self._reaction_failed("add", emoji, repr(exc))
        return True

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
    """Socket Mode request listener.

    ``events_api`` envelopes are settled through the inbound handler. ``interactive``
    envelopes (button presses) are acknowledged at once, then handled (ADR-0020).
    """

    def __init__(self, handler: InboundHandler, actions: ApprovalActions | None = None) -> None:
        self._handler = handler
        self._actions = actions
        self._running: set[asyncio.Task[None]] = set()

    async def __call__(self, client: AsyncBaseSocketModeClient, request: SocketModeRequest) -> None:
        if request.type == INTERACTIVE and self._actions is not None:
            await _acknowledge(client, request)
            task = asyncio.create_task(self._actions.handle(request.payload))
            self._running.add(task)
            task.add_done_callback(self._running.discard)
            return
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


def _delivery_error(method: str, exc: SlackApiError) -> SlackDeliveryError:
    error = _error_code(exc)
    status = exc.response.status_code
    transient = status == 429 or status >= 500 or error in TRANSIENT_ERRORS
    return SlackDeliveryError(f"{method}: {error}", transient=transient)


def _error_code(exc: SlackApiError) -> str:
    error = exc.response.get("error")
    return error if isinstance(error, str) else "unknown_error"


def _shown_as(persona: Persona | None) -> tuple[str | None, str | None]:
    """``username`` and ``icon_emoji`` for ``chat.postMessage`` and
    ``files.completeUploadExternal`` (chat:write.customize)."""
    if persona is None:
        return None, None
    icon = None if persona.icon_emoji is None else f":{persona.icon_emoji}:"
    return persona.name, icon
