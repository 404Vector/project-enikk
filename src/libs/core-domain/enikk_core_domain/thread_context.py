"""What else was posted in a thread since its agent last ran (ADR-0027).

A Slack thread belongs to one agent (I-11), but delegates and core also post into it.
The thread's agent never sees those posts in its own conversation, so they are put in
front of its next input. Only messages already posted in that thread are included.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from collections.abc import Sequence

    from enikk_core_domain.model import OutboxMessage

MAX_MESSAGES: Final = 20
MAX_PER_MESSAGE: Final = 4_000
MAX_INPUT: Final = 40_000
HEADER: Final = "[이 스레드에 그 사이 에닉의 다른 역할과 시스템이 올린 내용]"
SEPARATOR: Final = "[소유자의 새 메시지]"


def with_thread_context(text: str, posted: Sequence[OutboxMessage]) -> str:
    """``text`` preceded by ``posted`` (oldest first), within the input limit.

    When everything does not fit, the oldest posts are dropped first; the owner's
    message itself is never cut.
    """
    if not posted:
        return text
    budget = MAX_INPUT - len(text) - len(HEADER) - len(SEPARATOR) - 8
    entries: list[str] = []
    for message in reversed(posted):
        body = message.text
        if len(body) > MAX_PER_MESSAGE:
            body = body[: MAX_PER_MESSAGE - 1] + "…"
        entry = f"- {message.created_at:%H:%M} UTC\n{body}"
        if len(entry) + 2 > budget:
            break
        budget -= len(entry) + 2
        entries.append(entry)
    if not entries:
        return text
    return "\n\n".join([HEADER, *reversed(entries), SEPARATOR, text])
