"""Slack Block Kit messages: approval requests, deployment notices and the status card
of an agent PR (ADR-0020, ADR-0023, ADR-0032, ADR-0036)."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any, Final, Literal

from pydantic import BaseModel, ConfigDict

from enikk_contracts.approvals import (
    ApprovalOutdated,
    ApprovalRequested,
    ApprovalResolved,
    ChecksFailed,
    PullRequestCard,
    PullStage,
    PullStatusCard,
    ResolutionOutcome,
)
from enikk_contracts.deployments import (
    AppResult,
    BuildFailed,
    DeploymentFinished,
    PullLink,
    RevertOutcome,
    RevertResult,
)
from enikk_contracts.prompts import (
    PromotionRequested,
    PromotionResult,
    PromotionStatus,
)
from enikk_contracts.skills import is_skill

if TYPE_CHECKING:
    from enikk_contracts.digests import DigestFailed

APPROVE_ACTION: Final = "enikk.approve"
REJECT_ACTION: Final = "enikk.reject"
REVERT_ACTION: Final = "enikk.revert"
PROMOTE_PROMPT_ACTION: Final = "enikk.prompt.promote"
REJECT_PROMPT_ACTION: Final = "enikk.prompt.reject"
REVERT_PROMPT_ACTION: Final = "enikk.prompt.revert"
CANCEL_ACTION: Final = "enikk.cancel"
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
    accessory: Button | None = None


class Context(_Block):
    type: Literal["context"] = "context"
    elements: tuple[Text, ...]


class Button(_Block):
    type: Literal["button"] = "button"
    text: Text
    action_id: str
    value: str
    style: Literal["primary", "danger"]
    confirm: Confirm | None = None


class Actions(_Block):
    type: Literal["actions"] = "actions"
    block_id: str = "enikk.decision"
    elements: tuple[Button, ...]


class Confirm(_Block):
    title: Text
    text: Text
    confirm: Text
    deny: Text


Block = Section | Context | Actions


class Message(_Block):
    """Fallback ``text`` for notifications, and the blocks shown in Slack."""

    text: str
    blocks: tuple[Block, ...]

    def payload(self) -> list[dict[str, object]]:
        return [b.model_dump(exclude_none=True) for b in self.blocks]

    def json_blocks(self) -> list[dict[str, Any]]:
        """The blocks as JSON values, for updating a message in place."""
        return [b.model_dump(mode="json", exclude_none=True) for b in self.blocks]


_HEADING: Final = re.compile(r"^\s{0,3}#{1,6}\s+(.*?)\s*#*\s*$")
_BOLD_LINE: Final = re.compile(r"^\s*\*\*(.+?)\*\*:?\s*$")
_STRONG: Final = re.compile(r"\*\*(.+?)\*\*")


def approval_request(notice: ApprovalRequested) -> Message:
    pull = notice.pull
    approval = str(notice.approval_id)
    warning: tuple[Block, ...] = ()
    confirm = None
    if pull.sensitive:
        listed = "\n".join(f"• `{_escape(p)}`" for p in pull.sensitive[:MAX_FILE_LINES])
        hidden = len(pull.sensitive) - MAX_FILE_LINES
        if hidden > 0:
            listed += f"\n• …외 {hidden}개"
        warning = (
            _section(
                ":warning: *민감 경로 변경* — 승인 절차, 권한, 설계 기록 같은 파일을 바꿉니다. "
                f"스레드의 첫 댓글에서 이 파일들의 diff 를 확인해 주세요.\n{listed}"
            ),
        )
        confirm = Confirm(
            title=Text(type="plain_text", text="민감 경로 변경"),
            text=Text(
                type="plain_text",
                text=f"민감 경로 {len(pull.sensitive)}개를 바꾸는 PR 입니다. 정말 머지할까요?",
            ),
            confirm=Text(type="plain_text", text="승인하고 머지"),
            deny=Text(type="plain_text", text="취소"),
        )
    return Message(
        text=f"{'민감 경로 변경 ' if pull.sensitive else ''}PR 승인 요청: "
        f"#{pull.number} {pull.title}",
        blocks=(
            *_headline(pull, heading=":inbox_tray: *승인 요청*"),
            *warning,
            Actions(
                elements=(
                    Button(
                        text=Text(type="plain_text", text="승인하고 머지"),
                        action_id=APPROVE_ACTION,
                        value=approval,
                        style="primary",
                        confirm=confirm,
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
    """Thread replies under a request: the sensitive diff first (ADR-0024), then
    metadata, PR body, changed files and checks."""
    replies: list[Message] = []
    if pull.sensitive_diff:
        # A literal ``` inside the diff would end the code block early.
        pieces = _chunks(pull.sensitive_diff.replace("```", "`\u200b``"), SECTION_LIMIT - 20)
        replies += [
            Message(
                text=f"PR #{pull.number} 민감 경로 diff ({i}/{len(pieces)})",
                blocks=(
                    _section(
                        (":warning: *민감 경로 diff*\n" if i == 1 else "")
                        + f"```{_escape(piece)}```"
                    ),
                ),
            )
            for i, piece in enumerate(pieces, start=1)
        ]
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
    return (
        *replies,
        *(
            Message(
                text=f"PR #{pull.number} 상세 ({i}/{total})"
                if total > 1
                else f"PR #{pull.number} 상세",
                blocks=(_section(chunk),),
            )
            for i, chunk in enumerate(chunks, start=1)
        ),
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


def sent_to_dm_instead(message: Message, channel: str) -> Message:
    """``message`` with a line saying it could not be posted in ``channel``."""
    note = Context(elements=(Text(text=f"<#{channel}> 에 올리지 못해 DM 으로 보냅니다."),))
    return Message(text=message.text, blocks=(note, *message.blocks))


NOT_OWNER_TEXT: Final = "이 버튼은 소유자만 누를 수 있습니다. 아무것도 바뀌지 않았습니다."
RUNNING_TEXT: Final = "작업 중"
ALREADY_FINISHED_TEXT: Final = "이미 끝난 작업입니다. 아무것도 바뀌지 않았습니다."


def running(task_id: str) -> Message:
    """One line in the thread while a task runs, with the owner's [취소] button (ADR-0037 3b)."""
    return Message(
        text=RUNNING_TEXT,
        blocks=(
            Section(
                text=Text(text=RUNNING_TEXT),
                accessory=Button(
                    text=Text(type="plain_text", text="취소"),
                    action_id=CANCEL_ACTION,
                    value=task_id,
                    style="danger",
                ),
            ),
        ),
    )


def outdated(notice: ApprovalOutdated) -> Message:
    """The PR's message while a newer commit is being checked: no buttons."""
    return Message(
        text=f"PR #{notice.number}: 새 커밋 검사 중",
        blocks=(
            _section(
                f":hourglass_flowing_sand: *새 커밋 검사 중* "
                f"<{notice.url}|#{notice.number} {_escape(notice.title)}>\n"
                f"커밋 `{notice.head_sha[:7]}` 의 검사가 끝나면 "
                "이 메시지가 새 승인 요청으로 바뀝니다."
            ),
        ),
    )


def resolved(notice: ApprovalResolved) -> Message:
    """The PR's outcome; it replaces the approval message (and its buttons) if known."""
    line = (
        f"<{notice.url}|#{notice.number} {_escape(notice.title)}> — {OUTCOME_TEXT[notice.outcome]}"
    )
    if notice.detail:
        line += f"\n> {_escape(notice.detail)}"
    return Message(text=f"PR #{notice.number}: {notice.outcome}", blocks=(_section(line),))


def digest_failed(notice: DigestFailed) -> Message:
    """One line: the day's conversation summary could not be made (ADR-0035)."""
    then = "다음 날 실행 때 한 번 더 시도합니다." if notice.retry else "다시 시도하지 않습니다."
    line = (
        f":warning: {notice.day.isoformat()} 대화 요약을 만들지 못했습니다 — "
        f"{_escape(notice.reason)} ({then})"
    )
    return Message(text=f"{notice.day.isoformat()} 대화 요약 실패", blocks=(_section(line),))


def deployment_finished(notice: DeploymentFinished) -> Message:
    what = _what(notice.commit, notice.pull)
    kind = "롤백" if notice.rollback_of else "배포"
    if notice.succeeded:
        heading = f":rocket: *{kind} 성공* {what}"
        summary = f"{kind} 성공: {_plain(notice.commit, notice.pull)}"
    else:
        heading = f":rotating_light: *{kind} 실패* {what}"
        summary = f"{kind} 실패: {_plain(notice.commit, notice.pull)}"
    if notice.rollback_of:
        heading += f"\n되돌린 커밋 `{notice.rollback_of[:7]}`"
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
        if notice.rollback_of is None:
            blocks.append(_revert_button(notice.commit))
    return Message(text=summary, blocks=tuple(blocks))


def build_failed(notice: BuildFailed) -> Message:
    what = _what(notice.commit, notice.pull)
    text = (
        f":hammer_and_wrench: *이미지 빌드 실패* {what}\n"
        f"<{notice.run_url}|빌드 기록> · 결과 `{_escape(notice.conclusion)}`"
    )
    if notice.rollback_of:
        text = f":rotating_light: *롤백 실패* (되돌린 커밋 `{notice.rollback_of[:7]}`)\n" + text
    blocks: list[Block] = [
        _section(text),
        Context(elements=(Text(text="배포는 시작되지 않았고 이전 버전이 그대로 동작합니다."),)),
    ]
    if notice.rollback_of is None:
        blocks.append(_revert_button(notice.commit))
    return Message(
        text=f"이미지 빌드 실패: {_plain(notice.commit, notice.pull)}", blocks=tuple(blocks)
    )


def _revert_button(commit: str) -> Actions:
    return Actions(
        block_id="enikk.revert",
        elements=(
            Button(
                text=Text(type="plain_text", text="되돌리기"),
                action_id=REVERT_ACTION,
                value=commit,
                style="danger",
                confirm=Confirm(
                    title=Text(type="plain_text", text="되돌리기"),
                    text=Text(
                        type="plain_text",
                        text=f"커밋 {commit[:7]} 을 되돌리는 PR 을 엽니다. "
                        "머지는 평소처럼 승인을 거칩니다.",
                    ),
                    confirm=Text(type="plain_text", text="PR 열기"),
                    deny=Text(type="plain_text", text="취소"),
                ),
            ),
        ),
    )


REVERT_TEXT: Final = {
    RevertOutcome.OPENED: ":leftwards_arrow_with_hook: 되돌리는 PR 을 열었습니다.",
    RevertOutcome.EXISTING: ":leftwards_arrow_with_hook: 되돌리는 PR 이 이미 열려 있습니다.",
    RevertOutcome.REFUSED: ":warning: 자동으로 되돌릴 수 없습니다.",
}


def revert_requested(result: RevertResult, by: str) -> Context:
    text = f"{REVERT_TEXT[result.outcome]} (<@{by}>)"
    if result.pull is not None:
        text += f" <{result.pull.url}|#{result.pull.number}> — 검사가 끝나면 승인 요청이 옵니다."
    if result.detail:
        text += f"\n> {_escape(result.detail)}"
    if result.outcome is RevertOutcome.REFUSED:
        text += "\n에닉에게 이 커밋을 되돌려 달라고 요청할 수 있습니다."
    return Context(elements=(Text(text=text[:SECTION_LIMIT]),))


# ---------------------------------------------------------------- status card (ADR-0036)

MERGED_STAGES: Final = frozenset(
    {
        PullStage.MERGED,
        PullStage.DEPLOYED,
        PullStage.DEPLOY_FAILED,
        PullStage.BUILD_FAILED,
        PullStage.ROLLED_BACK,
        PullStage.ROLLBACK_FAILED,
    }
)
"""Stages after the merge. Builds and deployments are told in the deployment channel."""
CLOSED_TEXT: Final = {
    PullStage.REJECTED: ":no_entry_sign: 반려됨",
    PullStage.CLOSED: ":heavy_minus_sign: 닫힘",
}


def status_card(card: PullStatusCard, review_link: str | None, approval_channel: str) -> Message:
    """The PR's one line in the asking thread: a link to its approval thread only (not to
    GitHub), and only whether it was merged or rejected. Checks, approval and deployment
    stay in their channels; no buttons, no details (the owner's decision, 2026-11-24)."""
    if review_link is not None:
        line = f"<{review_link}|PR #{card.number} 승인 스레드>"
    else:
        line = f"PR #{card.number} · <#{approval_channel}>"
    outcome = _outcome(card.stage)
    if outcome is not None:
        line += f" · {outcome}"
    return Message(text=f"PR #{card.number}", blocks=(Context(elements=(Text(text=line),)),))


def _outcome(stage: PullStage) -> str | None:
    if stage in MERGED_STAGES:
        return ":white_check_mark: 머지됨"
    return CLOSED_TEXT.get(stage)


# ---------------------------------------------------------------- prompts (ADR-0032)

PROMOTION_TEXT: Final = {
    PromotionStatus.REQUESTED: ":hourglass_flowing_sand: 아직 결정되지 않았습니다.",
    PromotionStatus.PROMOTED: ":white_check_mark: production 으로 승격했습니다.",
    PromotionStatus.REJECTED: (
        ":no_entry_sign: 거절했습니다. Langfuse 는 바뀌지 않았고 후보 버전은 남아 있습니다."
    ),
    PromotionStatus.STALE: ":warning: 승격하지 않았습니다.",
    PromotionStatus.REVERTED: ":leftwards_arrow_with_hook: 이전 버전으로 되돌렸습니다.",
}


def promotion_request(notice: PromotionRequested) -> Message:
    """The approval channel: which prompt, from which version to which, why, and the buttons.
    The diff follows in the thread (``promotion_details``)."""
    promotion = str(notice.promotion_id)
    roles = ", ".join(notice.roles)
    used = f" ({_escape(roles)} 역할)" if roles else (" (스킬)" if is_skill(notice.prompt) else "")
    change = f"production {_version(notice.production_version)} → 후보 v{notice.candidate_version}"
    reason = "\n".join(
        f"> {line}" for line in _escape(notice.reason).splitlines()[:SUMMARY_LINES] if line
    )
    added = ", ".join(f"`{c.value}`" for c in notice.requires_added)
    warning = (
        (
            _section(
                f":warning: *권한 확장*: 이 후보는 {added} 를 새로 요구합니다. 승격하면 이 "
                "스킬을 불러온 세션이 core 에 그 동작을 요청할 수 있습니다(ADR-0037 §6)."
            ),
        )
        if notice.requires_added
        else ()
    )
    confirm = f"{notice.prompt} 의 production 을 v{notice.candidate_version} 로 옮깁니다." + (
        f" 새로 요구하는 권한: {', '.join(c.value for c in notice.requires_added)}."
        if notice.requires_added
        else ""
    )
    return Message(
        text=f"프롬프트 승격 요청: {notice.prompt} v{notice.candidate_version}"
        + (" (권한 확장)" if notice.requires_added else ""),
        blocks=(
            _section(
                f":memo: *프롬프트 승격 요청* `{_escape(notice.prompt)}`{used}\n{change}\n{reason}"
            ),
            *warning,
            Context(
                elements=(Text(text="본문과 Config 의 차이는 이 메시지의 스레드에 있습니다."),)
            ),
            Actions(
                block_id="enikk.prompt",
                elements=(
                    Button(
                        text=Text(type="plain_text", text="승격"),
                        action_id=PROMOTE_PROMPT_ACTION,
                        value=promotion,
                        style="primary",
                        confirm=Confirm(
                            title=Text(type="plain_text", text="프롬프트 승격"),
                            text=Text(type="plain_text", text=confirm),
                            confirm=Text(type="plain_text", text="승격"),
                            deny=Text(type="plain_text", text="취소"),
                        ),
                    ),
                    Button(
                        text=Text(type="plain_text", text="거절"),
                        action_id=REJECT_PROMPT_ACTION,
                        value=promotion,
                        style="danger",
                    ),
                ),
            ),
        ),
    )


def promotion_details(notice: PromotionRequested) -> tuple[Message, ...]:
    """Thread replies under a promotion request: the text diff, then the config diff."""
    replies: list[Message] = []
    for title, diff in (("본문 diff", notice.text_diff), ("Config diff", notice.config_diff)):
        if not diff.strip():
            replies.append(
                Message(
                    text=f"{notice.prompt} {title}",
                    blocks=(_section(f"*{title}*: 바뀐 것이 없습니다."),),
                )
            )
            continue
        pieces = _chunks(diff.replace("```", "`\u200b``"), SECTION_LIMIT - 40)
        replies += [
            Message(
                text=f"{notice.prompt} {title} ({i}/{len(pieces)})",
                blocks=(
                    _section(
                        (f"*{title}*" + (f" ({i}/{len(pieces)})" if len(pieces) > 1 else ""))
                        + f"\n```{_escape(piece)}```"
                    ),
                ),
            )
            for i, piece in enumerate(pieces, start=1)
        ]
    return tuple(replies)


def promotion_decided(promotion_id: str, result: PromotionResult, by: str) -> list[Block]:
    """Blocks that replace the buttons: what happened, and [되돌리기] after a promotion."""
    text = PROMOTION_TEXT[result.status]
    if not result.changed:
        text = f":information_source: 바뀐 것이 없습니다. 지금 상태: {text}"
    else:
        text += (
            f" `{_escape(result.prompt)}` {_version(result.production_version)} → "
            f"v{result.candidate_version}"
            if result.status is PromotionStatus.PROMOTED
            else ""
        )
    text += f" (<@{by}>)"
    if result.detail:
        text += f"\n> {_escape(result.detail)}"
    blocks: list[Block] = [Context(elements=(Text(text=text[:SECTION_LIMIT]),))]
    if result.status is PromotionStatus.PROMOTED and result.production_version is not None:
        blocks.append(
            Actions(
                block_id="enikk.prompt.revert",
                elements=(
                    Button(
                        text=Text(
                            type="plain_text", text=f"v{result.production_version} 로 되돌리기"
                        ),
                        action_id=REVERT_PROMPT_ACTION,
                        value=promotion_id,
                        style="danger",
                        confirm=Confirm(
                            title=Text(type="plain_text", text="프롬프트 되돌리기"),
                            text=Text(
                                type="plain_text",
                                text=f"{result.prompt} 의 production 을 "
                                f"v{result.production_version} 로 되돌립니다.",
                            ),
                            confirm=Text(type="plain_text", text="되돌리기"),
                            deny=Text(type="plain_text", text="취소"),
                        ),
                    ),
                ),
            )
        )
    return blocks


def _version(version: int | None) -> str:
    return "없음" if version is None else f"v{version}"


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
