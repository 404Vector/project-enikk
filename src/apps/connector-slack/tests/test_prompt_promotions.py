"""Prompt promotion requests in the owner's DM and the owner's buttons (ADR-0032)."""

from __future__ import annotations

import uuid
from typing import Any

import pydantic_core
import pytest

from enikk_connector_slack.approvals import ApprovalActions, NoticeRelay
from enikk_connector_slack.blocks import (
    PROMOTE_PROMPT_ACTION,
    REJECT_PROMPT_ACTION,
    REVERT_PROMPT_ACTION,
    SECTION_LIMIT,
    promotion_details,
)
from enikk_connector_slack.outbound import RelayPolicy
from enikk_connector_slack.testing import CONFIG, OWNER, FakeCore, FakeSlack
from enikk_contracts.approvals import OwnerNotice
from enikk_contracts.common import ConnectorKind, ConversationRef
from enikk_contracts.prompts import (
    PromotionAction,
    PromotionRequested,
    PromotionResult,
    PromotionStatus,
)
from enikk_core_client import CoreUnavailableError

pytestmark = pytest.mark.anyio

POLICY = RelayPolicy(wait_seconds=0, batch=10, backoff_max_seconds=1)
PROMOTION = uuid.UUID("87654321-4321-8765-4321-876543218765")
REQUEST = PromotionRequested(
    promotion_id=PROMOTION,
    prompt="assistant",
    roles=("assistant",),
    production_version=3,
    candidate_version=5,
    reason="답을 더 짧게\n두 번째 줄\n세 번째 줄은 본문에 없음",
    text_diff="--- production v3\n+++ candidate\n-You are enikk.\n+You are enikk. Be brief.",
    config_diff="",
    origin=ConversationRef(connector=ConnectorKind.SLACK, channel="C1", thread="9.0"),
)


def _all_text(blocks: object) -> str:
    return str(blocks)


def _plain(blocks: object) -> list[Any]:
    """Blocks as plain JSON values, to look inside them."""
    plain: list[Any] = pydantic_core.from_json(pydantic_core.to_json(blocks))
    return plain


async def test_request_goes_to_the_approval_channel_with_buttons_and_the_diff() -> None:
    core, slack = FakeCore(), FakeSlack()
    core.notices = [OwnerNotice(notice_id=uuid.uuid4(), body=REQUEST)]
    assert await NoticeRelay(core, slack, CONFIG, POLICY).relay_once() == 1

    [(channel, message)] = slack.messages
    assert channel == CONFIG.approval_channel
    shown = _all_text(message.payload())
    assert "`assistant`" in shown
    assert "production v3 → 후보 v5" in shown
    assert "답을 더 짧게" in shown
    assert "두 번째 줄" in shown
    assert "세 번째 줄" not in shown
    [buttons] = [b for b in _plain(message.payload()) if b["type"] == "actions"]
    ids = [e["action_id"] for e in buttons["elements"]]
    values = {e["value"] for e in buttons["elements"]}
    assert ids == [PROMOTE_PROMPT_ACTION, REJECT_PROMPT_ACTION]
    assert values == {str(PROMOTION)}
    assert "#" not in message.text

    [origin] = slack.posts
    assert origin[2].startswith("원래 요청 스레드: https://slack.test/archives/C1/")
    replies = [_all_text(m.payload()) for _, _, m in slack.replies]
    assert any("본문 diff" in r and "+You are enikk. Be brief." in r for r in replies)
    assert any("Config diff" in r and "바뀐 것이 없습니다" in r for r in replies)

    [(promotion_id, posted)] = core.promotions_posted
    assert promotion_id == PROMOTION
    assert posted.link is not None
    assert posted.link.startswith(f"https://slack.test/archives/{CONFIG.approval_channel}/")
    assert core.notices_acked


def test_a_long_diff_is_split_into_several_replies() -> None:
    long = REQUEST.model_copy(
        update={"text_diff": "\n".join(f"+line {i} " + "x" * 80 for i in range(200))}
    )
    replies = promotion_details(long)
    texts = [_all_text(r.payload()) for r in replies]
    diff_parts = [t for t in texts if "본문 diff" in t]
    assert len(diff_parts) > 1
    for reply in replies:
        for block in _plain(reply.payload()):
            assert len(block["text"]["text"]) <= SECTION_LIMIT


def _press(user: str, action: str, value: str = str(PROMOTION)) -> dict[str, object]:
    return {
        "type": "block_actions",
        "user": {"id": user},
        "container": {"type": "message", "channel_id": "D0OWNER", "message_ts": "1.0"},
        "message": {
            "text": "프롬프트 승격 요청",
            "blocks": [
                {"type": "section", "text": {"type": "mrkdwn", "text": "프롬프트"}},
                {"type": "actions", "elements": []},
            ],
        },
        "actions": [{"action_id": action, "value": value}],
    }


async def test_owner_promotion_reaches_core_and_offers_a_revert() -> None:
    core, slack = FakeCore(), FakeSlack()
    await ApprovalActions(core, slack, CONFIG).handle(_press(OWNER, PROMOTE_PROMPT_ACTION))
    [(promotion_id, decision)] = core.promotion_decisions
    assert promotion_id == PROMOTION
    assert (decision.action, decision.decided_by) == (PromotionAction.PROMOTE, OWNER)
    [(_, ts, blocks)] = slack.updates
    assert ts == "1.0"
    shown = _all_text(blocks)
    assert "production 으로 승격했습니다" in shown
    assert "최대 60초" in shown
    [actions] = [b for b in _plain(blocks) if b.get("type") == "actions"]
    [button] = actions["elements"]
    assert button["action_id"] == REVERT_PROMPT_ACTION
    assert button["value"] == str(PROMOTION)
    assert "v3 로 되돌리기" in str(button["text"])


@pytest.mark.parametrize(
    ("action", "expected"),
    [
        (REJECT_PROMPT_ACTION, PromotionAction.REJECT),
        (REVERT_PROMPT_ACTION, PromotionAction.REVERT),
    ],
)
async def test_reject_and_revert_reach_core_without_new_buttons(
    action: str, expected: PromotionAction
) -> None:
    core, slack = FakeCore(), FakeSlack()
    status = (
        PromotionStatus.REJECTED if expected is PromotionAction.REJECT else PromotionStatus.REVERTED
    )
    core.promotion_result = PromotionResult(
        status=status, changed=True, prompt="assistant", production_version=3, candidate_version=5
    )
    await ApprovalActions(core, slack, CONFIG).handle(_press(OWNER, action))
    [(_, decision)] = core.promotion_decisions
    assert decision.action is expected
    [(_, _, blocks)] = slack.updates
    assert not [b for b in blocks if b.get("type") == "actions"]


async def test_a_repeated_press_shows_that_nothing_changed() -> None:
    core, slack = FakeCore(), FakeSlack()
    core.promotion_result = PromotionResult(
        status=PromotionStatus.PROMOTED,
        changed=False,
        prompt="assistant",
        production_version=3,
        candidate_version=5,
        detail="이미 처리된 요청입니다.",
    )
    await ApprovalActions(core, slack, CONFIG).handle(_press(OWNER, PROMOTE_PROMPT_ACTION))
    [(_, _, blocks)] = slack.updates
    assert "바뀐 것이 없습니다" in _all_text(blocks)


async def test_someone_else_cannot_promote() -> None:
    core, slack = FakeCore(), FakeSlack()
    await ApprovalActions(core, slack, CONFIG).handle(_press("U0SOMEONE", PROMOTE_PROMPT_ACTION))
    assert core.promotion_decisions == []
    assert slack.updates == []
    [(_, user, _)] = slack.ephemerals
    assert user == "U0SOMEONE"


async def test_a_failure_keeps_the_buttons_and_asks_to_press_again() -> None:
    core, slack = FakeCore(), FakeSlack()
    core.fail_with = CoreUnavailableError("down")
    await ApprovalActions(core, slack, CONFIG).handle(_press(OWNER, PROMOTE_PROMPT_ACTION))
    assert slack.updates == []
    [(_, _, text)] = slack.posts
    assert "아무것도 바뀌지 않았습니다" in text


async def test_a_bad_button_value_is_ignored() -> None:
    core, slack = FakeCore(), FakeSlack()
    await ApprovalActions(core, slack, CONFIG).handle(
        _press(OWNER, PROMOTE_PROMPT_ACTION, value="not-a-uuid")
    )
    assert core.promotion_decisions == []


def test_a_skill_that_asks_for_more_actions_shows_a_warning() -> None:
    from enikk_connector_slack.blocks import promotion_request
    from enikk_contracts.skills import Capability

    plain = promotion_request(REQUEST)
    assert "권한 확장" not in _all_text(plain.payload())

    widened = REQUEST.model_copy(
        update={"prompt": "skill-developer", "roles": (), "requires_added": (Capability.PR_WRITE,)}
    )
    message = promotion_request(widened)
    shown = _all_text(message.payload())
    assert "(스킬)" in shown
    assert ":warning: *권한 확장*" in shown
    assert "`pr.write`" in shown
    assert "권한 확장" in message.text
    [buttons] = [b for b in _plain(message.payload()) if b["type"] == "actions"]
    assert "pr.write" in buttons["elements"][0]["confirm"]["text"]["text"]
