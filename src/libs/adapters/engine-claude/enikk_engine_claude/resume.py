"""Starting a new session when a thread's session cannot be resumed (ADR-0034).

The new session gets the end of the saved conversation in front of its input, as text.
Tool calls and their results are left out.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

MAX_PER_MESSAGE: Final = 2_000
MAX_HISTORY: Final = 12_000
HEADER: Final = (
    "[이 스레드의 이전 대화를 이어받지 못해 새 대화로 시작한다. "
    "아래는 저장된 이전 대화의 마지막 부분이다]"
)
EMPTY_HEADER: Final = "[이 스레드의 이전 대화를 이어받지 못해 새 대화로 시작한다]"
SEPARATOR: Final = "[새 메시지]"
LABELS: Final = {"user": "입력", "assistant": "답"}


def with_history(text: str, entries: Iterable[Mapping[str, Any]]) -> str:
    """``text`` preceded by the newest turns of ``entries`` (a CLI transcript) that fit."""
    turns = _turns(entries)
    budget = MAX_HISTORY
    kept: list[str] = []
    for role, body in reversed(turns):
        if len(body) > MAX_PER_MESSAGE:
            body = body[: MAX_PER_MESSAGE - 1] + "…"
        entry = f"- {LABELS[role]}:\n{body}"
        if len(entry) + 2 > budget:
            break
        budget -= len(entry) + 2
        kept.append(entry)
    if not kept:
        return f"{EMPTY_HEADER}\n\n{text}"
    return "\n\n".join([HEADER, *reversed(kept), SEPARATOR, text])


def _turns(entries: Iterable[Mapping[str, Any]]) -> list[tuple[str, str]]:
    """Main-thread text of user and assistant entries, consecutive ones of a role joined."""
    turns: list[tuple[str, str]] = []
    for entry in entries:
        role = entry.get("type")
        if role not in LABELS or entry.get("isSidechain") or entry.get("isMeta"):
            continue
        message = entry.get("message")
        body = _text(message.get("content") if isinstance(message, dict) else None)
        if not body:
            continue
        if turns and turns[-1][0] == role:
            turns[-1] = (role, f"{turns[-1][1]}\n{body}")
        else:
            turns.append((str(role), body))
    return turns


def _text(content: object) -> str:
    if isinstance(content, str):
        return content.strip()
    if not isinstance(content, list):
        return ""
    parts = [
        str(block.get("text", "")).strip()
        for block in content
        if isinstance(block, dict) and block.get("type") == "text"
    ]
    return "\n".join(p for p in parts if p)
