"""Which Slack messages become core events (I-01, I-15, ADR-0018)."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Final

from enikk_connector_slack.events import MessageRef
from enikk_contracts.common import ConnectorKind, ConversationRef
from enikk_contracts.events import InboundEvent

if TYPE_CHECKING:
    from enikk_connector_slack.config import SlackConfig
    from enikk_connector_slack.events import SlackMessage

APP_MENTION: Final = "app_mention"
MESSAGE: Final = "message"
DIRECT_MESSAGE: Final = "im"
MAX_TEXT: Final = 40_000

FILE_SHARE: Final = "file_share"
"""The subtype of a message with attached files (ADR-0042)."""

_log = logging.getLogger(__name__)


class Router:
    """Turns an owner's message into an ``InboundEvent``; everything else is ignored.

    - Direct message: always handled.
    - Mention of the bot: handled from the ``app_mention`` event.
    - Other thread reply in a channel: continues the thread's conversation if it has one.
    """

    def __init__(self, config: SlackConfig, bot_user_id: str) -> None:
        self._config = config
        self._mention = f"<@{bot_user_id}>"

    def route(self, message: SlackMessage) -> InboundEvent | None:
        if message.bot_id is not None or message.subtype not in (None, FILE_SHARE):
            return None
        if message.channel is None or message.ts is None or message.user is None:
            return None
        if message.user != self._config.owner:
            _log.debug("ignored a message from %s: not the owner", message.user)
            return None
        text = _with_files(message.text.replace(self._mention, "").strip(), message)[:MAX_TEXT]
        if not text:
            return None

        routes = self._config.routes
        if message.type == APP_MENTION:
            agent, continue_only = routes.mention, False
        elif message.type == MESSAGE and message.channel_type == DIRECT_MESSAGE:
            agent, continue_only = routes.direct_message, False
        elif message.type == MESSAGE and message.thread_ts is not None:
            if self._mention in message.text:
                return None  # the app_mention event carries it
            agent, continue_only = routes.mention, True
        else:
            return None

        ref = MessageRef(channel=message.channel, ts=message.ts)
        return InboundEvent(
            event_id=ref.event_id,
            conversation=ConversationRef(
                connector=ConnectorKind.SLACK,
                channel=message.channel,
                thread=message.thread_ts or message.ts,
            ),
            user=message.user,
            text=text,
            agent=agent,
            continue_only=continue_only,
        )


def _with_files(text: str, message: SlackMessage) -> str:
    """Tell the agent what was attached; it cannot read attachments yet (ADR-0042)."""
    if not message.files:
        return text
    names = ", ".join(f.name or "(이름 없음)" for f in message.files)
    note = f"[첨부 파일 {len(message.files)}개: {names}. 에닉은 아직 첨부 파일을 읽을 수 없다]"
    return f"{text}\n\n{note}" if text else note
