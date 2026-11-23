from __future__ import annotations

import logging
import uuid

import pytest

from enikk_connector_slack.approvals import ApprovalActions, NoticeRelay
from enikk_connector_slack.blocks import (
    APPROVE_ACTION,
    REJECT_ACTION,
    SECTION_LIMIT,
    SUMMARY_LIMIT,
    Actions,
    details,
    summary,
)
from enikk_connector_slack.outbound import RelayPolicy
from enikk_connector_slack.ports import SlackDeliveryError
from enikk_connector_slack.testing import CONFIG, OWNER, FakeCore, FakeSlack
from enikk_contracts.approvals import (
    ApprovalRequested,
    ApprovalResolved,
    ChangedFile,
    CheckResult,
    ChecksFailed,
    Decision,
    OwnerNotice,
    PullRequestCard,
    ResolutionOutcome,
)
from enikk_contracts.common import ConnectorKind, ConversationRef
from enikk_contracts.threads import ReviewThreadLinked
from enikk_core_client import CoreConflictError, CoreUnavailableError

pytestmark = pytest.mark.anyio

POLICY = RelayPolicy(wait_seconds=0, batch=10, backoff_max_seconds=1)
APPROVAL = uuid.UUID("12345678-1234-5678-1234-567812345678")
CARD = PullRequestCard(
    repository="404Vector/project-enikk",
    number=7,
    url="https://github.com/404Vector/project-enikk/pull/7",
    title="docs: add <a> line",
    author="enikk-deployer[bot]",
    branch="agent/abc",
    head_sha="1" * 40,
    body="원래 요청: README 에 한 줄",
    files=(ChangedFile(path="README.md", additions=1, deletions=0),),
    checks=(CheckResult(name="checks", conclusion="success"),),
    diff="+one line\n",
)


def _relay(core: FakeCore, slack: FakeSlack) -> NoticeRelay:
    return NoticeRelay(core, slack, CONFIG, POLICY)


async def test_request_goes_to_the_owner_dm_with_buttons_and_diff() -> None:
    core, slack = FakeCore(), FakeSlack()
    notice = OwnerNotice(
        notice_id=uuid.uuid4(), body=ApprovalRequested(approval_id=APPROVAL, pull=CARD)
    )
    core.notices.append(notice)
    assert await _relay(core, slack).relay_once() == 1

    [(channel, message)] = slack.messages
    assert channel == OWNER
    [actions] = [b for b in message.blocks if isinstance(b, Actions)]
    assert [e.action_id for e in actions.elements] == [APPROVE_ACTION, REJECT_ACTION]
    assert all(e.value == str(APPROVAL) for e in actions.elements)
    rendered = str(message.payload())
    assert "docs: add &lt;a&gt; line" in rendered
    assert "원래 요청: README 에 한 줄" in rendered
    assert CARD.url in rendered
    [(upload_channel, thread, filename, content)] = slack.uploads
    assert (upload_channel, thread) == ("D0OWNER", "1.0")
    assert filename == "pr-7-1111111.diff"
    assert content == "+one line\n"
    assert core.notices_acked == [notice.notice_id]


async def test_request_keeps_details_out_of_the_parent_and_in_its_thread() -> None:
    core, slack = FakeCore(), FakeSlack()
    core.notices.append(
        OwnerNotice(notice_id=uuid.uuid4(), body=ApprovalRequested(approval_id=APPROVAL, pull=CARD))
    )
    await _relay(core, slack).relay_once()

    [(_, message)] = slack.messages
    assert [b.type for b in message.blocks] == ["section", "section", "actions"]
    parent = str(message.payload())
    assert "README.md" not in parent
    assert "agent/abc" not in parent
    [(channel, thread, reply)] = slack.replies
    assert (channel, thread) == ("D0OWNER", "1.0")
    detail = str(reply.payload())
    assert "README.md" in detail
    assert "agent/abc" in detail
    assert "checks" in detail


async def test_long_details_are_split_into_several_replies() -> None:
    files = tuple(
        ChangedFile(path=f"src/{'deep/' * 8}module_{i:03}.py", additions=i, deletions=0)
        for i in range(100)
    )
    pull = CARD.model_copy(update={"body": "요약 문단\n\n" + "본문 " * 900, "files": files})
    core, slack = FakeCore(), FakeSlack()
    core.notices.append(
        OwnerNotice(notice_id=uuid.uuid4(), body=ApprovalRequested(approval_id=APPROVAL, pull=pull))
    )
    await _relay(core, slack).relay_once()

    assert len(slack.replies) > 1
    texts = [r.blocks[0].model_dump()["text"]["text"] for _, _, r in slack.replies]
    assert all(len(t) <= SECTION_LIMIT for t in texts)
    joined = "\n".join(texts)
    assert all(f.path in joined for f in files)
    assert slack.replies[0][2].text == f"PR #7 상세 (1/{len(texts)})"


async def test_failed_details_still_leave_the_request_and_its_buttons(
    caplog: pytest.LogCaptureFixture,
) -> None:
    core, slack = FakeCore(), FakeSlack()
    slack.fail_replies.append(SlackDeliveryError("chat.postMessage: boom", transient=True))
    notice = OwnerNotice(
        notice_id=uuid.uuid4(), body=ApprovalRequested(approval_id=APPROVAL, pull=CARD)
    )
    core.notices.append(notice)
    with caplog.at_level(logging.WARNING):
        assert await _relay(core, slack).relay_once() == 1

    [(_, message)] = slack.messages
    assert any(isinstance(b, Actions) for b in message.blocks)
    assert slack.replies == []
    assert len(slack.uploads) == 1
    assert core.notices_acked == [notice.notice_id]
    assert "could not post details of PR #7" in caplog.text


ORIGIN = ConversationRef(connector=ConnectorKind.SLACK, channel="C0ASK", thread="1700.1")


@pytest.mark.parametrize("failed", [False, True])
async def test_review_thread_and_asking_thread_link_each_other(failed: bool) -> None:
    core, slack = FakeCore(), FakeSlack()
    core.review_linked = ReviewThreadLinked(origin=ORIGIN, report="상세 보고: src/a.py 수정")
    body = ChecksFailed(pull=CARD) if failed else ApprovalRequested(approval_id=APPROVAL, pull=CARD)
    core.notices.append(OwnerNotice(notice_id=uuid.uuid4(), body=body))
    await _relay(core, slack).relay_once()

    [review] = core.reviews
    assert (review.repository, review.number, review.branch) == (CARD.repository, 7, "agent/abc")
    assert review.review_link == "https://slack.test/archives/D0OWNER/p10"
    [origin_link, report] = slack.posts
    assert origin_link == (
        "D0OWNER",
        "1.0",
        "원래 요청 스레드: https://slack.test/archives/C0ASK/p17001",
    )
    assert report[:2] == ("D0OWNER", "1.0")
    assert "상세 보고: src/a.py 수정" in report[2]
    assert report[2].startswith("*개발 역할 보고*")
    assert all(channel == "D0OWNER" for channel, _, _ in slack.posts)


async def test_links_are_skipped_without_permalinks_and_core_failures_do_not_stop_the_dm(
    caplog: pytest.LogCaptureFixture,
) -> None:
    core, slack = FakeCore(), FakeSlack()
    slack.no_permalinks = True
    core.review_linked = ReviewThreadLinked(origin=ORIGIN)
    core.notices.append(
        OwnerNotice(notice_id=uuid.uuid4(), body=ApprovalRequested(approval_id=APPROVAL, pull=CARD))
    )
    await _relay(core, slack).relay_once()
    [review] = core.reviews
    assert review.review_link is None
    assert slack.posts == []

    core, slack = FakeCore(), FakeSlack()
    core.review_fails = CoreUnavailableError("down")
    notice = OwnerNotice(
        notice_id=uuid.uuid4(), body=ApprovalRequested(approval_id=APPROVAL, pull=CARD)
    )
    core.notices.append(notice)
    with caplog.at_level(logging.WARNING):
        assert await _relay(core, slack).relay_once() == 1
    assert len(slack.messages) == 1
    assert slack.replies
    assert core.notices_acked == [notice.notice_id]
    assert "could not link the review thread of PR #7" in caplog.text


def test_summary_prefers_a_summary_section() -> None:
    body = "## 원래 요청\n긴 요청 문장\n\n## 요약\n짧은 **요약**\n둘째 줄\n셋째 줄\n\n## 변경\n- a"
    assert summary(body) == "짧은 요약\n둘째 줄"


def test_summary_falls_back_to_the_first_paragraph_and_truncates() -> None:
    assert summary("# 제목\n\n첫 문단\n\n둘째 문단") == "첫 문단"
    long = summary("가" * 500)
    assert len(long) == SUMMARY_LIMIT
    assert long.endswith("…")
    assert summary("") == ""


def test_details_render_markdown_headings_as_bold() -> None:
    [reply] = details(CARD.model_copy(update={"body": "## 변경 내용\n**굵게** 설명"}))
    text = reply.blocks[0].model_dump()["text"]["text"]
    assert "*변경 내용*" in text
    assert "*굵게* 설명" in text
    assert "#" not in text


async def test_failed_checks_and_resolutions_have_no_buttons() -> None:
    core, slack = FakeCore(), FakeSlack()
    core.notices += [
        OwnerNotice(notice_id=uuid.uuid4(), body=ChecksFailed(pull=CARD)),
        OwnerNotice(
            notice_id=uuid.uuid4(),
            body=ApprovalResolved(
                approval_id=APPROVAL,
                repository=CARD.repository,
                number=7,
                url=CARD.url,
                title=CARD.title,
                outcome=ResolutionOutcome.SUPERSEDED,
            ),
        ),
    ]
    assert await _relay(core, slack).relay_once() == 2
    assert [b["type"] for b in slack.messages[0][1].payload()] == ["section", "section"]
    assert len(slack.replies) == 1
    for _, message in slack.messages:
        assert all(b["type"] != "actions" for b in message.payload())


def _press(user: str, action: str = APPROVE_ACTION) -> dict[str, object]:
    return {
        "type": "block_actions",
        "user": {"id": user},
        "container": {"type": "message", "channel_id": "D0OWNER", "message_ts": "1.0"},
        "message": {
            "text": "PR 승인 요청",
            "blocks": [
                {"type": "section", "text": {"type": "mrkdwn", "text": "PR"}},
                {"type": "actions", "elements": []},
            ],
        },
        "actions": [{"action_id": action, "value": str(APPROVAL)}],
    }


async def test_owner_approval_reaches_core_and_removes_the_buttons() -> None:
    core, slack = FakeCore(), FakeSlack()
    await ApprovalActions(core, slack, CONFIG).handle(_press(OWNER))
    [(approval_id, request)] = core.decisions
    assert approval_id == APPROVAL
    assert request.decision is Decision.APPROVE
    assert request.decided_by == OWNER
    [(channel, ts, blocks)] = slack.updates
    assert (channel, ts) == ("D0OWNER", "1.0")
    assert [b["type"] for b in blocks] == ["section", "context"]
    assert "머지" in str(blocks[-1])


async def test_reject_button_sends_reject() -> None:
    core, slack = FakeCore(), FakeSlack()
    core.decision_result = core.decision_result.model_copy(
        update={"outcome": ResolutionOutcome.REJECTED}
    )
    await ApprovalActions(core, slack, CONFIG).handle(_press(OWNER, REJECT_ACTION))
    assert core.decisions[0][1].decision is Decision.REJECT


async def test_anyone_but_the_owner_is_ignored() -> None:
    core, slack = FakeCore(), FakeSlack()
    await ApprovalActions(core, slack, CONFIG).handle(_press("U0SOMEONE"))
    assert core.decisions == []
    assert slack.updates == []
    [(_, user, _)] = slack.ephemerals
    assert user == "U0SOMEONE"


async def test_core_refusal_is_told_in_the_thread_and_keeps_the_buttons() -> None:
    core, slack = FakeCore(), FakeSlack()
    core.fail_with = CoreConflictError("busy", status=409)
    await ApprovalActions(core, slack, CONFIG).handle(_press(OWNER))
    assert slack.updates == []
    [(channel, thread, text)] = slack.posts
    assert (channel, thread) == ("D0OWNER", "1.0")
    assert "다시 눌러" in text


async def test_unrelated_payloads_are_ignored() -> None:
    core, slack = FakeCore(), FakeSlack()
    await ApprovalActions(core, slack, CONFIG).handle({"type": "view_submission"})
    assert core.decisions == []


async def test_sensitive_changes_warn_confirm_and_show_their_diff_first() -> None:
    core, slack = FakeCore(), FakeSlack()
    card = CARD.model_copy(
        update={
            "sensitive": ("docs/adr/0099-x.md",),
            "sensitive_diff": "--- docs/adr/0099-x.md\n+adr",
        }
    )
    core.notices.append(
        OwnerNotice(notice_id=uuid.uuid4(), body=ApprovalRequested(approval_id=APPROVAL, pull=card))
    )
    await _relay(core, slack).relay_once()
    [(_, parent)] = slack.messages
    (_, _, first_reply) = slack.replies[0]
    assert parent.text.startswith("민감 경로 변경")
    assert "민감 경로 변경" in str(parent.payload())
    [actions] = [b for b in parent.blocks if isinstance(b, Actions)]
    approve = next(e for e in actions.elements if e.action_id == APPROVE_ACTION)
    assert approve.confirm is not None
    reject = next(e for e in actions.elements if e.action_id == REJECT_ACTION)
    assert reject.confirm is None
    assert "+adr" in str(first_reply.payload())


async def test_ordinary_changes_have_no_confirmation() -> None:
    core, slack = FakeCore(), FakeSlack()
    core.notices.append(
        OwnerNotice(notice_id=uuid.uuid4(), body=ApprovalRequested(approval_id=APPROVAL, pull=CARD))
    )
    await _relay(core, slack).relay_once()
    [actions] = [b for b in slack.messages[0][1].blocks if isinstance(b, Actions)]
    assert all(e.confirm is None for e in actions.elements)
    assert "confirm" not in str(slack.messages[0][1].payload())


async def test_a_replacing_request_updates_the_message_in_place() -> None:
    from enikk_contracts.approvals import ApprovalOutdated, ReviewMessage

    core, slack = FakeCore(), FakeSlack()
    old = ReviewMessage(channel="D0OWNER", ts="5.0")
    core.notices += [
        OwnerNotice(
            notice_id=uuid.uuid4(),
            body=ApprovalOutdated(
                repository=CARD.repository,
                number=7,
                url=CARD.url,
                title=CARD.title,
                head_sha="2" * 40,
                replaces=old,
            ),
        ),
        OwnerNotice(
            notice_id=uuid.uuid4(),
            body=ApprovalRequested(
                approval_id=APPROVAL,
                pull=CARD.model_copy(update={"head_sha": "2" * 40}),
                replaces=old,
            ),
        ),
    ]
    assert await _relay(core, slack).relay_once() == 2
    assert slack.messages == []
    [(c1, ts1, waiting), (c2, ts2, request)] = slack.updates
    assert (c1, ts1) == (c2, ts2) == ("D0OWNER", "5.0")
    assert "검사 중" in str(waiting)
    assert all(b["type"] != "actions" for b in waiting)
    assert any(b["type"] == "actions" for b in request)
    assert any("갱신" in text for (_, _, text) in slack.posts)
    [review] = core.reviews
    assert (review.channel, review.ts, review.updated) == ("D0OWNER", "5.0", True)
