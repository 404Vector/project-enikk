"""Slack Block Kit messages for the owner's DM (ADR-0020)."""

from __future__ import annotations

import re
from typing import Final, Literal

from pydantic import BaseModel, ConfigDict

from enikk_contracts.approvals import (
    ApprovalRequested,
    ApprovalResolved,
    ChecksFailed,
    PullRequestCard,
    ResolutionOutcome,
)

APPROVE_ACTION: Final = "enikk.approve"
REJECT_ACTION: Final = "enikk.reject"
MAX_FILE_LINES: Final = 20
SECTION_LIMIT: Final = 2_900
SUMMARY_LIMIT: Final = 200
SUMMARY_LINES: Final = 2
SUMMARY_HEADINGS: Final = frozenset({"요약", "summary", "개요"})
PASSING: Final = frozenset({"success", "neutral", "skipped"})

OUTCOME_TEXT: Final = {
    ResolutionOutcome.MERGED: ":white_check_mark: 승인했고 머지했습니다.",
    ResolutionOutcome.REJECTED: ":no_entry_sign: 반려했습니다. PR 을 닫았습니다.",
    ResolutionOutcome.SUPERSEDED: (
        ":arrows_counterclockwise: 새 커밋이 올라와 이 요청은 무효입니다. "
        "검사가 끝나면 새 요청을 보냅니다."
    ),
    ResolutionOutcome.BLOCKED: ":warning: 승인했지만 GitHub 가 머지를 거부했습니다.",
    ResolutionOutcome.CLOSED: ":heavy_minus_sign: PR 이 닫혔거나 이미 처리되었습니다.",
}


class _Block(BaseModel):
    model_config = ConfigDict(frozen=True)


class Text(_Block):
    type: Literal["mrkdwn", "plain_text"] = "mrkdwn"
    text: str


class Section(_Block):
    type: Literal["section"] = "section"
    text: Text


class Context(_Block):
    type: Literal["context"] = "context"
    elements: tuple[Text, ...]


class Button(_Block):
    type: Literal["button"] = "button"
    text: Text
    action_id: str
    value: str
    style: Literal["primary", "danger"]


class Actions(_Block):
    type: Literal["actions"] = "actions"
    block_id: str = "enikk.decision"
    elements: tuple[Button, ...]


Block = Section | Context | Actions


class Message(_Block):
    """Fallback ``text`` for notifications, and the blocks shown in Slack."""

    text: str
    blocks: tuple[Block, ...]

    def payload(self) -> list[dict[str, object]]:
        return [b.model_dump() for b in self.blocks]


_HEADING: Final = re.compile(r"^\s{0,3}#{1,6}\s+(.*?)\s*#*\s*$")
_BOLD_LINE: Final = re.compile(r"^\s*\*\*(.+?)\*\*:?\s*$")
_STRONG: Final = re.compile(r"\*\*(.+?)\*\*")


def approval_request(notice: ApprovalRequested) -> Message:
    pull = notice.pull
    approval = str(notice.approval_id)
    return Message(
        text=f"PR 승인 요청: #{pull.number} {pull.title}",
        blocks=(
            *_headline(pull, heading=":inbox_tray: *승인 요청*"),
            Actions(
                elements=(
                    Button(
                        text=Text(type="plain_text", text="승인하고 머지"),
                        action_id=APPROVE_ACTION,
                        value=approval,
                        style="primary",
                    ),
                    Button(
                        text=Text(type="plain_text", text="반려"),
                        action_id=REJECT_ACTION,
                        value=approval,
                        style="danger",
                    ),
                )
            ),
        ),
    )


def checks_failed(notice: ChecksFailed) -> Message:
    pull = notice.pull
    return Message(
        text=f"PR 검사 실패: #{pull.number} {pull.title}",
        blocks=_headline(pull, heading=":x: *검사 실패* (승인할 수 없습니다)"),
    )


def details(pull: PullRequestCard) -> tuple[Message, ...]:
    """Thread replies under a request: metadata, PR body, changed files and checks."""
    parts = [
        f"`{pull.repository}` · 작성자 `{pull.author}` · 브랜치 `{pull.branch}` "
        f"· 커밋 `{pull.head_sha[:7]}`"
    ]
    if pull.body.strip():
        parts.append(_mrkdwn(pull.body))
    if pull.files:
        added = sum(f.additions for f in pull.files)
        removed = sum(f.deletions for f in pull.files)
        lines = [f"*바뀐 파일* {len(pull.files) + pull.more_files}개 (+{added} -{removed})"]
        lines += [f"• `{f.path}` +{f.additions} -{f.deletions}" for f in pull.files]
        if pull.more_files:
            lines.append(f"• …외 {pull.more_files}개")
        parts.append("\n".join(lines))
    if pull.checks:
        marks = "\n".join(f"{_mark(c.conclusion)} {_escape(c.name)}" for c in pull.checks)
        parts.append(f"*검사*\n{marks}")
    chunks = _chunks("\n\n".join(parts), SECTION_LIMIT)
    total = len(chunks)
    return tuple(
        Message(
            text=f"PR #{pull.number} 상세 ({i}/{total})"
            if total > 1
            else f"PR #{pull.number} 상세",
            blocks=(_section(chunk),),
        )
        for i, chunk in enumerate(chunks, start=1)
    )


def summary(body: str) -> str:
    """One or two lines from a PR body: its summary section, else its first paragraph."""
    paragraphs = _paragraphs(body)
    chosen = next(
        (text for title, text in paragraphs if title.lower() in SUMMARY_HEADINGS and text), None
    )
    if chosen is None:
        chosen = next((text for _, text in paragraphs if text), "")
    lines = [line.strip() for line in chosen.splitlines() if line.strip()][:SUMMARY_LINES]
    text = _STRONG.sub(r"\1", "\n".join(lines))
    if len(text) > SUMMARY_LIMIT:
        text = text[: SUMMARY_LIMIT - 1].rstrip() + "…"
    return text


def resolved(notice: ApprovalResolved) -> Message:
    line = (
        f"<{notice.url}|#{notice.number} {_escape(notice.title)}> — {OUTCOME_TEXT[notice.outcome]}"
    )
    if notice.detail:
        line += f"\n> {_escape(notice.detail)}"
    return Message(text=f"PR #{notice.number}: {notice.outcome}", blocks=(_section(line),))


def decided(outcome: ResolutionOutcome, detail: str, by: str) -> Context:
    text = f"{OUTCOME_TEXT[outcome]} (<@{by}>)"
    if detail:
        text += f" — {_escape(detail)}"
    return Context(elements=(Text(text=text[:SECTION_LIMIT]),))


def _headline(pull: PullRequestCard, *, heading: str) -> tuple[Block, ...]:
    blocks: list[Block] = [_section(f"{heading} <{pull.url}|#{pull.number} {_escape(pull.title)}>")]
    short = summary(pull.body)
    if short:
        blocks.append(_section("\n".join(f"> {line}" for line in _escape(short).splitlines())))
    return tuple(blocks)


def _paragraphs(body: str) -> list[tuple[str, str]]:
    """``(heading, text)`` for each paragraph. ``heading`` is the nearest one above it."""
    found: list[tuple[str, str]] = []
    heading = ""
    current: list[str] = []

    def flush() -> None:
        if current:
            found.append((heading, "\n".join(current)))
            current.clear()

    for line in body.splitlines():
        title = _HEADING.match(line) or _BOLD_LINE.match(line)
        if title:
            flush()
            heading = title.group(1).strip().rstrip(":")
        elif line.strip():
            current.append(line)
        else:
            flush()
    flush()
    return found


def _mrkdwn(body: str) -> str:
    lines = []
    for line in _escape(body).splitlines():
        title = _HEADING.match(line)
        lines.append(f"*{title.group(1)}*" if title else _STRONG.sub(r"*\1*", line))
    return "\n".join(lines)


def _chunks(text: str, limit: int) -> list[str]:
    chunks: list[str] = []
    current = ""
    for line in text.splitlines():
        while len(line) > limit:
            if current:
                chunks.append(current)
                current = ""
            chunks.append(line[:limit])
            line = line[limit:]
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) > limit:
            chunks.append(current)
            current = line
        else:
            current = candidate
    if current.strip():
        chunks.append(current)
    return chunks


def _mark(conclusion: str) -> str:
    return ":white_check_mark:" if conclusion in PASSING else ":x:"


def _section(text: str) -> Section:
    return Section(text=Text(text=text[:SECTION_LIMIT]))


def _escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
