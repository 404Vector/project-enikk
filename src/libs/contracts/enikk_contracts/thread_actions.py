"""What a session may do in the thread it answers, besides replying (ADR-0042)."""

from __future__ import annotations

import uuid
from datetime import timedelta
from enum import StrEnum
from typing import Annotated, Final, Literal

from pydantic import AwareDatetime, Field, HttpUrl, StringConstraints

from enikk_contracts.base import Contract

MAX_FILE_BYTES: Final = 10 * 1024 * 1024
MAX_FILES_PER_TASK: Final = 10
MAX_EMOJI: Final = 5000
MAX_SCHEDULED: Final = 50
"""Scheduled messages waiting at once, across all threads."""
MAX_SCHEDULE_AHEAD: Final = timedelta(days=120)
SCHEDULED_TEXT_CHARS: Final = 4000

FileName = Annotated[
    str, StringConstraints(min_length=1, max_length=200, pattern=r"^[^/\\\x00-\x1f]+$")
]
FileTitle = Annotated[str, StringConstraints(min_length=1, max_length=200, strip_whitespace=True)]
EmojiName = Annotated[str, StringConstraints(pattern=r"^[a-z0-9_+'\-]{1,100}$")]
Base64Text = Annotated[
    str, StringConstraints(min_length=1, max_length=(MAX_FILE_BYTES + 2) // 3 * 4)
]


class ThreadFileRequest(Contract):
    """Post a file of the session's workspace to the thread the task answers."""

    lease_id: uuid.UUID
    filename: FileName
    title: FileTitle
    content_base64: Base64Text


class ThreadReactionRequest(Contract):
    """Put an emoji on the owner's message that the task answers."""

    lease_id: uuid.UUID
    name: EmojiName


class ThreadActionDone(Contract):
    action: Literal["file", "reaction"]
    note: Annotated[str, StringConstraints(max_length=500)] = ""
    """Something the session should know, e.g. that the emoji is not a custom one."""


# ------------------------------------------------------------ workspace (connector → core)


class SlackWorkspaceReport(Contract):
    """What the connector knows of the workspace, sent at start and every few minutes.

    ``emoji`` is ``None`` when the app may not read the custom emoji (no ``emoji:read``).
    """

    url: HttpUrl
    emoji: Annotated[tuple[EmojiName, ...], Field(max_length=MAX_EMOJI)] | None = None


class SlackWorkspaceReceived(Contract):
    emoji: Annotated[int, Field(ge=0)] | None


# ------------------------------------------------------------ links and emoji (session)


class LinkTarget(StrEnum):
    THREAD = "thread"
    """The first message of the thread the task answers."""
    REQUEST = "request"
    """The owner's message the task answers."""
    PULL_REQUEST = "pull_request"
    """The approval message of a pull request of the platform's repository."""


class MessageLinkRequest(Contract):
    lease_id: uuid.UUID
    target: LinkTarget
    number: Annotated[int, Field(ge=1)] | None = None
    """The pull request's number, for ``PULL_REQUEST`` only."""


class MessageLink(Contract):
    url: HttpUrl


class EmojiQuery(Contract):
    lease_id: uuid.UUID


class EmojiList(Contract):
    """The workspace's custom emoji names; Slack's standard ones are not listed."""

    known: bool
    """False until the connector reported a list (it needs ``emoji:read``)."""
    names: Annotated[tuple[EmojiName, ...], Field(max_length=MAX_EMOJI)] = ()


# ------------------------------------------------------------ scheduled messages


class ScheduleTarget(StrEnum):
    THREAD = "thread"
    """A reply in the thread the task answers."""
    DM = "dm"
    """A new message in the owner's DM with the app."""


ScheduledText = Annotated[
    str, StringConstraints(min_length=1, max_length=SCHEDULED_TEXT_CHARS, strip_whitespace=True)
]


class ScheduleMessageRequest(Contract):
    lease_id: uuid.UUID
    text: ScheduledText
    at: AwareDatetime
    target: ScheduleTarget = ScheduleTarget.THREAD


class ScheduledMessage(Contract):
    message_id: uuid.UUID
    at: AwareDatetime
    target: ScheduleTarget
    text: ScheduledText


class ScheduledQuery(Contract):
    lease_id: uuid.UUID


class ScheduledMessages(Contract):
    messages: Annotated[tuple[ScheduledMessage, ...], Field(max_length=MAX_SCHEDULED)]


class CancelScheduledRequest(Contract):
    lease_id: uuid.UUID
    message_id: uuid.UUID


class ScheduledCancelled(Contract):
    cancelled: bool
