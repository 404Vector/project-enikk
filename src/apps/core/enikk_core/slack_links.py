"""Slack message links built from the workspace address the connector reported (ADR-0042)."""

from __future__ import annotations

from typing import Final

from enikk_kernel.models import FrozenModel

SLACK_EVENT_PREFIX: Final = "slack:"


class SlackMessage(FrozenModel):
    channel: str
    ts: str
    thread: str | None = None
    """The thread's first message when this one is a reply in it."""


def event_message(event_id: str) -> SlackMessage | None:
    """The Slack message behind a connector event ID (``slack:<channel>:<ts>``)."""
    if not event_id.startswith(SLACK_EVENT_PREFIX):
        return None
    channel, _, ts = event_id.removeprefix(SLACK_EVENT_PREFIX).partition(":")
    if not channel or not ts or ":" in ts:
        return None
    return SlackMessage(channel=channel, ts=ts)


def permalink(workspace_url: str, message: SlackMessage) -> str:
    """Slack's own permalink form: ``archives/<channel>/p<ts without the dot>``."""
    base = workspace_url if workspace_url.endswith("/") else workspace_url + "/"
    link = f"{base}archives/{message.channel}/p{message.ts.replace('.', '')}"
    if message.thread is not None and message.thread != message.ts:
        link += f"?thread_ts={message.thread}&cid={message.channel}"
    return link
