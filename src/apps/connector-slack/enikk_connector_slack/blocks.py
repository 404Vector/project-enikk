"""Slack Block Kit messages for the owner's DM (ADR-0020, ADR-0023)."""

from __future__ import annotations

from typing import Final, Literal

from pydantic import BaseModel, ConfigDict

from enikk_contracts.approvals import (
    ApprovalRequested,
    ApprovalResolved,
    ChecksFailed,
    PullRequestCard,
    ResolutionOutcome,
)
from enikk_contracts.deployments import (
    AppResult,
    BuildFailed,
    DeploymentFinished,
    PullLink,
)

APPROVE_ACTION: Final = "enikk.approve"
REJECT_ACTION: Final = "enikk.reject"
MAX_FILE_LINES: Final = 20
SECTION_LIMIT: Final = 2_900
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


APP_RESULT_TEXT: Final = {
    AppResult.SUCCEEDED: ":white_check_mark:",
    AppResult.FAILED: ":x:",
    AppResult.REPLACED: ":fast_forward: 다음 배포로 대체",
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


def approval_request(notice: ApprovalRequested) -> Message:
    pull = notice.pull
    approval = str(notice.approval_id)
    return Message(
        text=f"PR 승인 요청: #{pull.number} {pull.title}",
        blocks=(
            *_describe(pull, heading=":inbox_tray: *승인 요청*"),
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
        blocks=_describe(pull, heading=":x: *검사 실패* (승인할 수 없습니다)"),
    )


def resolved(notice: ApprovalResolved) -> Message:
    line = (
        f"<{notice.url}|#{notice.number} {_escape(notice.title)}> — {OUTCOME_TEXT[notice.outcome]}"
    )
    if notice.detail:
        line += f"\n> {_escape(notice.detail)}"
    return Message(text=f"PR #{notice.number}: {notice.outcome}", blocks=(_section(line),))


def deployment_finished(notice: DeploymentFinished) -> Message:
    what = _what(notice.commit, notice.pull)
    if notice.succeeded:
        heading = f":rocket: *배포 성공* {what}"
        summary = f"배포 성공: {_plain(notice.commit, notice.pull)}"
    else:
        heading = f":rotating_light: *배포 실패* {what}"
        summary = f"배포 실패: {_plain(notice.commit, notice.pull)}"
    lines = [heading]
    for app in notice.apps:
        line = f"• `{app.app}` {APP_RESULT_TEXT[app.result]}"
        if app.detail:
            line += f" — {_escape(app.detail)}"
        lines.append(line)
    blocks: list[Block] = [_section("\n".join(lines))]
    if not notice.succeeded:
        blocks.append(
            Context(
                elements=(
                    Text(
                        text="새 Pod 가 준비되지 않은 앱은 이전 버전이 계속 동작합니다. "
                        "동기화가 실패한 앱은 일부만 바뀌었을 수 있습니다."
                    ),
                )
            )
        )
    return Message(text=summary, blocks=tuple(blocks))


def build_failed(notice: BuildFailed) -> Message:
    what = _what(notice.commit, notice.pull)
    text = (
        f":hammer_and_wrench: *이미지 빌드 실패* {what}\n"
        f"<{notice.run_url}|빌드 기록> · 결과 `{_escape(notice.conclusion)}`"
    )
    return Message(
        text=f"이미지 빌드 실패: {_plain(notice.commit, notice.pull)}",
        blocks=(
            _section(text),
            Context(elements=(Text(text="배포는 시작되지 않았고 이전 버전이 그대로 동작합니다."),)),
        ),
    )


def _what(commit: str, pull: PullLink | None) -> str:
    if pull is None:
        return f"커밋 `{commit[:7]}`"
    return f"<{pull.url}|#{pull.number} {_escape(pull.title)}> · 커밋 `{commit[:7]}`"


def _plain(commit: str, pull: PullLink | None) -> str:
    return f"#{pull.number} {pull.title}" if pull else commit[:7]


def decided(outcome: ResolutionOutcome, detail: str, by: str) -> Context:
    text = f"{OUTCOME_TEXT[outcome]} (<@{by}>)"
    if detail:
        text += f" — {_escape(detail)}"
    return Context(elements=(Text(text=text[:SECTION_LIMIT]),))


def _describe(pull: PullRequestCard, *, heading: str) -> tuple[Block, ...]:
    blocks: list[Block] = [
        _section(
            f"{heading} <{pull.url}|#{pull.number} {_escape(pull.title)}>\n"
            f"`{pull.repository}` · 작성자 `{pull.author}` · 브랜치 `{pull.branch}` "
            f"· 커밋 `{pull.head_sha[:7]}`"
        )
    ]
    if pull.body.strip():
        quoted = "\n".join(f"> {line}" for line in _escape(pull.body).splitlines()[:15])
        blocks.append(_section(quoted))
    if pull.files:
        added = sum(f.additions for f in pull.files)
        removed = sum(f.deletions for f in pull.files)
        lines = [f"*바뀐 파일* {len(pull.files) + pull.more_files}개 (+{added} -{removed})"]
        lines += [
            f"• `{f.path}` +{f.additions} -{f.deletions}" for f in pull.files[:MAX_FILE_LINES]
        ]
        hidden = len(pull.files) - MAX_FILE_LINES + pull.more_files
        if hidden > 0:
            lines.append(f"• …외 {hidden}개")
        blocks.append(_section("\n".join(lines)))
    if pull.checks:
        marks = ", ".join(f"{_mark(c.conclusion)} {_escape(c.name)}" for c in pull.checks)
        blocks.append(Context(elements=(Text(text=f"검사: {marks}"[:SECTION_LIMIT]),)))
    return tuple(blocks)


def _mark(conclusion: str) -> str:
    return ":white_check_mark:" if conclusion in PASSING else ":x:"


def _section(text: str) -> Section:
    return Section(text=Text(text=text[:SECTION_LIMIT]))


def _escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
