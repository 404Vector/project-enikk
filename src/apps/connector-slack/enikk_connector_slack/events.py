"""Slack payloads the connector reads, and the reference to a Slack message."""

from __future__ import annotations

from typing import Final, Literal

from pydantic import BaseModel, ConfigDict, JsonValue

from enikk_kernel.models import FrozenModel

EVENT_ID_PREFIX: Final = "slack:"


class SlackPayload(BaseModel):
    """Slack sends many fields; only the declared ones are read."""

    model_config = ConfigDict(frozen=True, extra="ignore")


class SlackFile(SlackPayload):
    id: str = ""
    name: str = ""
    size: int = 0
    url_private_download: str | None = None


class SlackMessage(SlackPayload):
    """A ``message`` or ``app_mention`` event."""

    type: str
    channel: str | None = None
    channel_type: str | None = None
    user: str | None = None
    text: str = ""
    ts: str | None = None
    thread_ts: str | None = None
    bot_id: str | None = None
    subtype: str | None = None
    files: tuple[SlackFile, ...] = ()


class EventCallback(SlackPayload):
    """Payload of a Socket Mode ``events_api`` envelope."""

    type: str
    event: SlackMessage


class _Id(SlackPayload):
    id: str


class _Action(SlackPayload):
    action_id: str
    value: str = ""


class _Container(SlackPayload):
    channel_id: str
    message_ts: str


class _InteractiveMessage(SlackPayload):
    text: str = ""
    blocks: tuple[dict[str, JsonValue], ...] = ()
    """Kept as Slack sent them, to redraw the message without its buttons."""


class BlockActions(SlackPayload):
    """Payload of a Socket Mode ``interactive`` envelope for a button press."""

    type: Literal["block_actions"]
    user: _Id
    container: _Container
    message: _InteractiveMessage = _InteractiveMessage()
    actions: tuple[_Action, ...] = ()

    @property
    def channel_id(self) -> str:
        return self.container.channel_id

    @property
    def message_ts(self) -> str:
        return self.container.message_ts

    @property
    def message_text(self) -> str:
        return self.message.text or "PR 승인 요청"

    @property
    def message_blocks(self) -> tuple[dict[str, JsonValue], ...]:
        return self.message.blocks


class PostedMessage(FrozenModel):
    channel: str
    ts: str


class AuthIdentity(SlackPayload):
    """Result of ``auth.test`` for the bot token."""

    user_id: str


class MessageRef(FrozenModel):
    """A Slack message. Its ``event_id`` is the core idempotency key (ADR-0018)."""

    channel: str
    ts: str

    @property
    def event_id(self) -> str:
        return f"{EVENT_ID_PREFIX}{self.channel}:{self.ts}"

    @classmethod
    def parse(cls, event_id: str) -> MessageRef | None:
        if not event_id.startswith(EVENT_ID_PREFIX):
            return None
        channel, _, ts = event_id.removeprefix(EVENT_ID_PREFIX).partition(":")
        if not channel or not ts:
            return None
        return cls(channel=channel, ts=ts)
