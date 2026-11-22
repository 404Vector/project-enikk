"""Slack payloads the connector reads, and the reference to a Slack message."""

from __future__ import annotations

from typing import Final

from pydantic import BaseModel, ConfigDict

from enikk_kernel.models import FrozenModel

EVENT_ID_PREFIX: Final = "slack:"


class SlackPayload(BaseModel):
    """Slack sends many fields; only the declared ones are read."""

    model_config = ConfigDict(frozen=True, extra="ignore")


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


class EventCallback(SlackPayload):
    """Payload of a Socket Mode ``events_api`` envelope."""

    type: str
    event: SlackMessage


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
