"""Approval requests in the approval channel, and one status card in the asking thread
(ADR-0036)."""

from __future__ import annotations

import logging
import uuid
from pathlib import Path

import pytest

from enikk_connector_slack.approvals import ApprovalActions, NoticeRelay
from enikk_connector_slack.blocks import APPROVE_ACTION, NOT_OWNER_TEXT, Actions, Context
from enikk_connector_slack.config import DEFAULT_APPROVAL_CHANNEL, SlackConfig
from enikk_connector_slack.outbound import RelayPolicy
from enikk_connector_slack.ports import SlackDeliveryError
from enikk_connector_slack.testing import CONFIG, OWNER, FakeCore, FakeSlack
from enikk_contracts.approvals import (
    ApprovalRequested,
    ApprovalResolved,
    OwnerNotice,
    PullRequestCard,
    PullStage,
    PullStatusCard,
    ResolutionOutcome,
    ReviewMessage,
)
from enikk_contracts.common import ConnectorKind, ConversationRef
from enikk_contracts.threads import ReviewThreadLinked
from enikk_kernel.errors import ConfigurationError

pytestmark = pytest.mark.anyio

POLICY = RelayPolicy(wait_seconds=0, batch=10, backoff_max_seconds=1)
APPROVALS = CONFIG.approval_channel
RELEASE = CONFIG.deploy_notify_channel
SLACK_YAML = Path(__file__).resolve().parents[4] / "config" / "slack.yaml"
APPROVAL = uuid.UUID("12345678-1234-5678-1234-567812345678")
REPO = "404Vector/project-enikk"
URL = "https://github.com/404Vector/project-enikk/pull/7"
SHA = "1" * 40
PULL = PullRequestCard(
    repository=REPO,
    number=7,
    url=URL,
    title="feat: x",
    author="enikk-deployer[bot]",
    branch="agent/abc",
    head_sha=SHA,
)
# The developer was asked in a thread of the owner's DM with the bot.
ORIGIN = ConversationRef(connector=ConnectorKind.SLACK, channel="D0ASK", thread="1700.1")


def _notice(body: object) -> OwnerNotice:
    return OwnerNotice.model_validate({"notice_id": uuid.uuid4(), "body": body})


def _card(stage: PullStage, **update: object) -> PullStatusCard:
    card = PullStatusCard(
        repository=REPO, number=7, url=URL, title="feat: x", origin=ORIGIN, stage=stage
    )
    return card.model_copy(update=update)


async def _relay(core: FakeCore, slack: FakeSlack, config: SlackConfig = CONFIG) -> int:
    return await NoticeRelay(core, slack, config, POLICY).relay_once()


def _types(blocks: object) -> list[str]:
    assert isinstance(blocks, list)
    return [b["type"] for b in blocks]


# ---------------------------------------------------------------- configuration


def test_the_approval_channel_defaults_and_can_be_set() -> None:
    assert DEFAULT_APPROVAL_CHANNEL == "C0CL7HZ5UQ1"
    assert SlackConfig.load(SLACK_YAML).approval_channel == "C0CL7HZ5UQ1"
    bare = SlackConfig.parse("owner: U0OWNER\nroutes: {direct_message: a, mention: a}\n")
    assert bare.approval_channel == "C0CL7HZ5UQ1"
    assert bare.with_approval_channel(None) is bare
    changed = bare.with_approval_channel("G0PRIVATE")
    assert changed.approval_channel == "G0PRIVATE"
    assert changed.deploy_notify_channel == bare.deploy_notify_channel
    with pytest.raises(ConfigurationError, match="approval channel"):
        bare.with_approval_channel("#approvals")


# ---------------------------------------------------------------- approval requests


async def test_approval_requests_go_to_the_configured_channel() -> None:
    core, slack = FakeCore(), FakeSlack()
    core.notices.append(_notice(ApprovalRequested(approval_id=APPROVAL, pull=PULL)))
    assert await _relay(core, slack, CONFIG.with_approval_channel("C0OTHER")) == 1
    [(channel, message)] = slack.messages
    assert channel == "C0OTHER"
    assert any(isinstance(b, Actions) for b in message.blocks)


@pytest.mark.parametrize(
    "error",
    [
        SlackDeliveryError("chat.postMessage: not_in_channel", transient=False),
        SlackDeliveryError("chat.postMessage: channel_not_found", transient=False),
        SlackDeliveryError("chat.postMessage: ratelimited", transient=True),
    ],
)
async def test_a_channel_failure_sends_the_request_to_the_owner_dm(
    error: SlackDeliveryError, caplog: pytest.LogCaptureFixture
) -> None:
    core, slack = FakeCore(), FakeSlack()
    slack.fail_channels[APPROVALS] = error
    notice = _notice(ApprovalRequested(approval_id=APPROVAL, pull=PULL))
    core.notices.append(notice)
    with caplog.at_level(logging.ERROR):
        assert await _relay(core, slack) == 1

    [(channel, message)] = slack.messages
    assert channel == OWNER
    first = message.blocks[0]
    assert isinstance(first, Context)
    assert "올리지 못해 DM 으로 보냅니다" in first.elements[0].text
    assert any(isinstance(b, Actions) for b in message.blocks)
    [review] = core.reviews
    assert review.channel == "D0OWNER"
    assert core.notices_acked == [notice.notice_id]
    assert f"in {APPROVALS}, sending it to the owner's DM" in caplog.text


async def test_a_failed_dm_fallback_is_retried_when_transient() -> None:
    core, slack = FakeCore(), FakeSlack()
    slack.fail_channels[APPROVALS] = SlackDeliveryError("x: not_in_channel", transient=False)
    slack.fail_channels[OWNER] = SlackDeliveryError("x: ratelimited", transient=True)
    core.notices.append(_notice(ApprovalRequested(approval_id=APPROVAL, pull=PULL)))
    with pytest.raises(SlackDeliveryError):
        await _relay(core, slack)
    assert core.notices_acked == []


async def test_a_resolution_replaces_the_approval_message_and_its_buttons() -> None:
    core, slack = FakeCore(), FakeSlack()
    resolved = ApprovalResolved(
        approval_id=APPROVAL,
        repository=REPO,
        number=7,
        url=URL,
        title="feat: x",
        outcome=ResolutionOutcome.CLOSED,
        replaces=ReviewMessage(channel=APPROVALS, ts="5.0"),
    )
    core.notices.append(_notice(resolved))
    assert await _relay(core, slack) == 1
    assert slack.messages == []
    [(channel, ts, blocks)] = slack.updates
    assert (channel, ts) == (APPROVALS, "5.0")
    assert "actions" not in _types(blocks)
    assert "닫혔" in str(blocks)


def _press(user: str) -> dict[str, object]:
    return {
        "type": "block_actions",
        "user": {"id": user},
        "container": {"type": "message", "channel_id": APPROVALS, "message_ts": "5.0"},
        "message": {
            "text": "PR 승인 요청",
            "blocks": [
                {"type": "section", "text": {"type": "mrkdwn", "text": "PR"}},
                {"type": "actions", "elements": []},
            ],
        },
        "actions": [{"action_id": APPROVE_ACTION, "value": str(APPROVAL)}],
    }


async def test_in_the_channel_only_the_owner_can_approve() -> None:
    core, slack = FakeCore(), FakeSlack()
    await ApprovalActions(core, slack, CONFIG).handle(_press("U0SOMEONE"))
    assert core.decisions == []
    assert slack.updates == []
    assert slack.messages == []
    assert slack.posts == []
    assert slack.ephemerals == [(APPROVALS, "U0SOMEONE", NOT_OWNER_TEXT)]

    await ApprovalActions(core, slack, CONFIG).handle(_press(OWNER))
    [(_, request)] = core.decisions
    assert request.decided_by == OWNER
    [(channel, ts, blocks)] = slack.updates
    assert (channel, ts) == (APPROVALS, "5.0")
    assert "actions" not in _types(blocks)


# ---------------------------------------------------------------- status card


async def test_the_first_card_is_posted_in_the_asking_thread_without_buttons() -> None:
    core, slack = FakeCore(), FakeSlack()
    review = ReviewMessage(channel=APPROVALS, ts="5.0")
    core.notices.append(_notice(_card(PullStage.AWAITING_APPROVAL, commit=SHA, review=review)))
    assert await _relay(core, slack) == 1

    assert slack.messages == []
    [(channel, thread, card)] = slack.replies
    assert (channel, thread) == (ORIGIN.channel, ORIGIN.thread)
    assert not any(isinstance(b, Actions) for b in card.blocks)
    shown = str(card.payload())
    assert "승인 대기" in shown
    assert "1111111" in shown
    # The card links to the approval message: the asking thread → the approval channel.
    assert f"https://slack.test/archives/{APPROVALS}/p50" in shown
    assert slack.posts == []
    assert slack.updates == []
    # Core learns where the card is, so later stages update it.
    [posted] = core.acks[-1].cards
    assert (posted.repository, posted.number) == (REPO, 7)
    assert (posted.channel, posted.ts) == (ORIGIN.channel, f"{ORIGIN.thread}.1")


@pytest.mark.parametrize("stage", [PullStage.MERGED, PullStage.DEPLOYED, PullStage.CLOSED])
async def test_later_stages_update_the_card_without_new_messages(stage: PullStage) -> None:
    core, slack = FakeCore(), FakeSlack()
    existing = ReviewMessage(channel=ORIGIN.channel, ts="1700.2")
    core.notices.append(_notice(_card(stage, commit=SHA, card=existing)))
    assert await _relay(core, slack) == 1

    assert slack.messages == []
    assert slack.replies == []
    assert slack.posts == []
    [(channel, ts, blocks)] = slack.updates
    assert (channel, ts) == (ORIGIN.channel, "1700.2")
    assert "actions" not in _types(blocks)
    assert core.acks[-1].cards == ()


@pytest.mark.parametrize(
    ("stage", "where"),
    [
        (PullStage.DEPLOY_FAILED, RELEASE),
        (PullStage.BUILD_FAILED, RELEASE),
        (PullStage.ROLLED_BACK, RELEASE),
        (PullStage.ROLLBACK_FAILED, RELEASE),
        (PullStage.CHECKS_FAILED, None),
        (PullStage.BLOCKED, None),
    ],
)
async def test_failures_update_the_card_and_add_one_line(stage: PullStage, where: str) -> None:
    core, slack = FakeCore(), FakeSlack()
    existing = ReviewMessage(channel=ORIGIN.channel, ts="1700.2")
    core.notices.append(_notice(_card(stage, commit=SHA, card=existing, detail="enikk-core")))
    assert await _relay(core, slack) == 1

    [(channel, ts, blocks)] = slack.updates
    assert (channel, ts) == (ORIGIN.channel, "1700.2")
    [(channel, thread, alert)] = slack.replies
    assert (channel, thread) == (ORIGIN.channel, ORIGIN.thread)
    shown = str(alert.payload())
    assert "PR #7" in shown
    assert "enikk-core" in shown
    assert not any(isinstance(b, Actions) for b in alert.blocks)
    if where is not None:
        assert f"<#{where}>" in shown
        assert f"<#{where}>" in str(blocks)
    assert slack.messages == []


async def test_deployment_details_stay_in_the_deployment_channel() -> None:
    core, slack = FakeCore(), FakeSlack()
    existing = ReviewMessage(channel=ORIGIN.channel, ts="1700.2")
    core.notices.append(_notice(_card(PullStage.DEPLOYED, commit=SHA, card=existing)))
    await _relay(core, slack)
    [(_, _, blocks)] = slack.updates
    shown = str(blocks)
    assert "배포됨" in shown
    assert f"<#{RELEASE}>" in shown
    assert "enikk-runtime-claude" not in shown


async def test_a_card_that_cannot_be_posted_is_dropped_unless_transient(
    caplog: pytest.LogCaptureFixture,
) -> None:
    core, slack = FakeCore(), FakeSlack()
    slack.fail_replies.append(SlackDeliveryError("x: channel_not_found", transient=False))
    core.notices.append(_notice(_card(PullStage.AWAITING_APPROVAL)))
    with caplog.at_level(logging.ERROR):
        assert await _relay(core, slack) == 1
    assert core.acks[-1].cards == ()
    assert "dropped status card" in caplog.text

    core, slack = FakeCore(), FakeSlack()
    slack.fail_replies.append(SlackDeliveryError("x: ratelimited", transient=True))
    core.notices.append(_notice(_card(PullStage.AWAITING_APPROVAL)))
    with pytest.raises(SlackDeliveryError):
        await _relay(core, slack)
    assert core.notices_acked == []


async def test_both_threads_link_each_other() -> None:
    """Approval thread → asking thread by its first reply; asking thread → approval
    message through the card."""
    core, slack = FakeCore(), FakeSlack()
    core.review_linked = ReviewThreadLinked(origin=ORIGIN)
    core.notices.append(_notice(ApprovalRequested(approval_id=APPROVAL, pull=PULL)))
    await _relay(core, slack)
    [(channel, _)] = slack.messages
    assert channel == APPROVALS
    [(post_channel, post_thread, text)] = slack.posts
    assert (post_channel, post_thread) == (APPROVALS, "1.0")
    assert text == "원래 요청 스레드: https://slack.test/archives/D0ASK/p17001"
    [review] = core.reviews
    assert (review.channel, review.ts) == (APPROVALS, "1.0")

    assert review.channel is not None
    assert review.ts is not None
    review_ref = ReviewMessage(channel=review.channel, ts=review.ts)
    core.notices.append(_notice(_card(PullStage.AWAITING_APPROVAL, review=review_ref)))
    await _relay(core, slack)
    [*_, (_, _, card)] = slack.replies
    assert f"https://slack.test/archives/{APPROVALS}/p10" in str(card.payload())
