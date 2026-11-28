"""Request cards between agents in the asking thread and the owner's buttons (ADR-0054)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import pydantic_core
import pytest

from enikk_connector_slack.approvals import ApprovalActions, NoticeRelay
from enikk_connector_slack.blocks import APPROVE_ASK_ACTION, REJECT_ASK_ACTION
from enikk_connector_slack.config import AgentChannel, Persona
from enikk_connector_slack.outbound import RelayPolicy
from enikk_connector_slack.testing import CONFIG, OWNER, FakeCore, FakeSlack
from enikk_contracts.approvals import OwnerNotice
from enikk_contracts.common import ConnectorKind, ConversationRef
from enikk_contracts.delegation import AskCard, AskResult, AskStatus

pytestmark = pytest.mark.anyio

POLICY = RelayPolicy(wait_seconds=0, batch=10, backoff_max_seconds=1)
REQUEST_ID = uuid.UUID("87654321-4321-8765-4321-876543218765")
RITA = Persona(name="리타", icon_emoji="rita")
CHANNELS = CONFIG.model_copy(
    update={"agent_channels": (AgentChannel(channel="C0RITA0001", agent="rita", persona=RITA),)}
)
CARD = AskCard(
    request_id=REQUEST_ID,
    requester="rita",
    agent="enikk",
    request="Unity 도구에 새 기능을 더해 주세요.\n" + "자세히. " * 1000,
    reason="제 도구로는 할 수 없습니다",
    origin=ConversationRef(connector=ConnectorKind.SLACK, channel="C0RITA0001", thread="9.0"),
    expires_at=datetime(2026, 11, 29, 12, tzinfo=UTC),
)


def _plain(blocks: object) -> list[Any]:
    plain: list[Any] = pydantic_core.from_json(pydantic_core.to_json(blocks))
    return plain


async def test_the_card_goes_to_the_asking_thread_under_its_persona() -> None:
    core, slack = FakeCore(), FakeSlack()
    core.notices = [OwnerNotice(notice_id=uuid.uuid4(), body=CARD)]
    assert await NoticeRelay(core, slack, CHANNELS, POLICY).relay_once() == 1

    assert slack.messages == []
    [(channel, thread, message)] = slack.replies
    assert (channel, thread) == ("C0RITA0001", "9.0")
    assert slack.personas == [("C0RITA0001", RITA)]
    shown = str(message.payload())
    assert "rita → enikk 요청" in shown
    assert "제 도구로는 할 수 없습니다" in shown
    assert "Unity 도구에 새 기능을 더해 주세요." in shown
    assert "<!date^" in shown
    blocks = _plain(message.payload())
    assert len(blocks) <= 50
    assert all(len(b.get("text", {}).get("text", "")) <= 3000 for b in blocks)
    [buttons] = [b for b in blocks if b["type"] == "actions"]
    assert [e["action_id"] for e in buttons["elements"]] == [APPROVE_ASK_ACTION, REJECT_ASK_ACTION]
    assert {e["value"] for e in buttons["elements"]} == {str(REQUEST_ID)}


def _press(user: str, action: str) -> dict[str, object]:
    return {
        "type": "block_actions",
        "user": {"id": user},
        "container": {"type": "message", "channel_id": "C0RITA0001", "message_ts": "9.5"},
        "message": {
            "text": "요청",
            "blocks": [
                {"type": "section", "text": {"type": "mrkdwn", "text": "요청"}},
                {"type": "actions", "elements": []},
            ],
        },
        "actions": [{"action_id": action, "value": str(REQUEST_ID)}],
    }


@pytest.mark.parametrize(
    ("action", "approve", "status", "text"),
    [
        (APPROVE_ASK_ACTION, True, AskStatus.APPROVED, "허가했습니다"),
        (REJECT_ASK_ACTION, False, AskStatus.REJECTED, "거절했습니다"),
    ],
)
async def test_the_owners_button_reaches_core_and_replaces_the_buttons(
    action: str, approve: bool, status: AskStatus, text: str
) -> None:
    core, slack = FakeCore(), FakeSlack()
    core.ask_result = AskResult(status=status, changed=True, detail="상세")
    await ApprovalActions(core, slack, CONFIG).handle(_press(OWNER, action))
    [(request_id, decision)] = core.ask_decisions
    assert (request_id, decision.approve, decision.decided_by) == (REQUEST_ID, approve, OWNER)
    [(_, ts, blocks)] = slack.updates
    assert ts == "9.5"
    assert text in str(blocks)
    assert not [b for b in blocks if b.get("type") == "actions"]


async def test_someone_else_cannot_press() -> None:
    core, slack = FakeCore(), FakeSlack()
    await ApprovalActions(core, slack, CONFIG).handle(_press("U0OTHER", APPROVE_ASK_ACTION))
    assert core.ask_decisions == []
