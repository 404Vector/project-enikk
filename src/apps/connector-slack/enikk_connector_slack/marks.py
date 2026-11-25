"""Reactions that show a request's state and what its task did (ADR-0018, ADR-0042)."""

from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING, Annotated, Final

from pydantic import StringConstraints

from enikk_kernel.models import FrozenModel

if TYPE_CHECKING:
    from enikk_connector_slack.events import MessageRef
    from enikk_connector_slack.ports import SlackApi

EmojiName = Annotated[str, StringConstraints(pattern=r"^[a-z0-9_+'\-]{1,100}$")]


class Mark(StrEnum):
    WAITING = "waiting"
    """Accepted; no worker has taken the task yet (ADR-0037 3b)."""
    RUNNING = "running"
    REPLIED = "replied"
    FAILED = "failed"
    CANCELLED = "cancelled"
    REMEMBERED = "remembered"
    """The task kept a new memory note (ADR-0028)."""
    MEMORY_UPDATED = "memory_updated"
    FORGOTTEN = "forgotten"


STANDARD: Final[dict[Mark, str]] = {
    Mark.WAITING: "hourglass_flowing_sand",
    Mark.RUNNING: "eyes",
    Mark.REPLIED: "white_check_mark",
    Mark.FAILED: "x",
    Mark.CANCELLED: "no_entry_sign",
    Mark.REMEMBERED: "bookmark",
    Mark.MEMORY_UPDATED: "pencil2",
    Mark.FORGOTTEN: "wastebasket",
}
"""Slack's own emoji, used when a custom one is not set or not in the workspace."""


class Reactions(FrozenModel):
    """Custom emoji of the workspace for each mark, from ``config/slack.yaml``."""

    waiting: EmojiName | None = None
    running: EmojiName | None = None
    replied: EmojiName | None = None
    failed: EmojiName | None = None
    cancelled: EmojiName | None = None
    remembered: EmojiName | None = None
    memory_updated: EmojiName | None = None
    forgotten: EmojiName | None = None

    def custom(self, mark: Mark) -> str | None:
        name: str | None = getattr(self, mark.value)
        return name


class Marker:
    """Puts marks on messages: the custom emoji, or Slack's own when it is unknown."""

    def __init__(self, slack: SlackApi, reactions: Reactions) -> None:
        self._slack = slack
        self._reactions = reactions

    async def mark(self, message: MessageRef, mark: Mark) -> None:
        custom = self._reactions.custom(mark)
        if custom is not None and await self._slack.react(message, custom):
            return
        await self._slack.react(message, STANDARD[mark])

    async def unmark(self, message: MessageRef, mark: Mark) -> None:
        custom = self._reactions.custom(mark)
        if custom is not None:
            await self._slack.unreact(message, custom)
        await self._slack.unreact(message, STANDARD[mark])
