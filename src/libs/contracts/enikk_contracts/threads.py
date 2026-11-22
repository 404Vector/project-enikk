"""Slack connector ↔ core: linking a PR's review thread with the thread that asked for it."""

from __future__ import annotations

from typing import Annotated

from pydantic import Field, StringConstraints

from enikk_contracts.base import Contract
from enikk_contracts.common import ConversationRef, NonEmptyText

Link = Annotated[str, StringConstraints(min_length=1, max_length=500)]


class ReviewThreadOpened(Contract):
    """The owner's review thread of a pull request was posted in their DM.

    ``review_link`` is that thread's permalink, ``None`` if Slack did not give one.
    """

    repository: Annotated[str, StringConstraints(min_length=1, max_length=200)]
    number: Annotated[int, Field(ge=1)]
    branch: Annotated[str, StringConstraints(min_length=1, max_length=255)]
    review_link: Link | None = None
    channel: Annotated[str, StringConstraints(min_length=1, max_length=255)] | None = None
    ts: Annotated[str, StringConstraints(min_length=1, max_length=255)] | None = None
    """The DM message, so the next commit of this PR updates it instead of a new one."""
    updated: bool = False
    """An existing message was updated for a new commit; the asking thread is not told again."""


class ReviewThreadLinked(Contract):
    """Where the pull request was asked for, and the agent's report for the review thread.

    Both are ``None`` when the pull request was not made for a delegated request.
    """

    origin: ConversationRef | None = None
    report: NonEmptyText | None = None
