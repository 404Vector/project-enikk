from __future__ import annotations

import uuid

import pytest

from enikk_connector_slack.approvals import ApprovalActions, NoticeRelay
from enikk_connector_slack.blocks import APPROVE_ACTION, REJECT_ACTION, Actions
from enikk_connector_slack.outbound import RelayPolicy
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
from enikk_core_client import CoreConflictError

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
    assert "README.md" in rendered
    assert CARD.url in rendered
    [(upload_channel, thread, filename, content)] = slack.uploads
    assert (upload_channel, thread) == ("D0OWNER", "1.0")
    assert filename == "pr-7-1111111.diff"
    assert content == "+one line\n"
    assert core.notices_acked == [notice.notice_id]


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
