"""Note change requests in the approval channel and the owner's buttons (ADR-0051 §4)."""

from __future__ import annotations

import uuid
from typing import Any

import pydantic_core
import pytest

from enikk_connector_slack.approvals import ApprovalActions, NoticeRelay
from enikk_connector_slack.blocks import APPLY_NOTE_ACTION, REJECT_NOTE_ACTION
from enikk_connector_slack.outbound import RelayPolicy
from enikk_connector_slack.testing import CONFIG, OWNER, FakeCore, FakeSlack
from enikk_contracts.approvals import OwnerNotice
from enikk_contracts.common import ConnectorKind, ConversationRef
from enikk_contracts.memory import (
    NoteChangeAction,
    NoteChangeRequested,
    NoteChangeResult,
    NoteChangeStatus,
)

pytestmark = pytest.mark.anyio

POLICY = RelayPolicy(wait_seconds=0, batch=10, backoff_max_seconds=1)
CHANGE = uuid.UUID("12345678-1234-5678-1234-567812345678")
REQUEST = NoteChangeRequested(
    change_id=CHANGE,
    action=NoteChangeAction.PUBLISH,
    path="vault/Garden/꼬리 지연.md",
    reason="검토를 마쳐서 발행합니다",
    published_before=False,
    published_after=True,
    excerpt="---\ndraft: true\n---\n# 꼬리 지연\n\n본문",
    origin=ConversationRef(connector=ConnectorKind.SLACK, channel="C1", thread="9.0"),
)


def _plain(blocks: object) -> list[Any]:
    plain: list[Any] = pydantic_core.from_json(pydantic_core.to_json(blocks))
    return plain


async def test_request_goes_to_the_approval_channel_with_buttons_and_the_note() -> None:
    core, slack = FakeCore(), FakeSlack()
    core.notices = [OwnerNotice(notice_id=uuid.uuid4(), body=REQUEST)]
    assert await NoticeRelay(core, slack, CONFIG, POLICY).relay_once() == 1

    [(channel, message)] = slack.messages
    assert channel == CONFIG.approval_channel
    shown = str(message.payload())
    assert "노트 발행 요청" in shown
    assert "공개 안 됨 → 블로그에 공개" in shown
    assert "검토를 마쳐서 발행합니다" in shown
    [buttons] = [b for b in _plain(message.payload()) if b["type"] == "actions"]
    assert [e["action_id"] for e in buttons["elements"]] == [APPLY_NOTE_ACTION, REJECT_NOTE_ACTION]
    assert {e["value"] for e in buttons["elements"]} == {str(CHANGE)}
    assert any("본문" in str(m.payload()) for _, _, m in slack.replies)
    [(change_id, posted)] = core.notes_posted
    assert change_id == CHANGE
    assert posted.link is not None


def _press(user: str, action: str) -> dict[str, object]:
    return {
        "type": "block_actions",
        "user": {"id": user},
        "container": {"type": "message", "channel_id": "C0APPROVAL", "message_ts": "1.0"},
        "message": {
            "text": "노트 발행 요청",
            "blocks": [
                {"type": "section", "text": {"type": "mrkdwn", "text": "노트"}},
                {"type": "actions", "elements": []},
            ],
        },
        "actions": [{"action_id": action, "value": str(CHANGE)}],
    }


@pytest.mark.parametrize(
    ("action", "apply", "status", "text"),
    [
        (APPLY_NOTE_ACTION, True, NoteChangeStatus.DONE, "실행했습니다"),
        (REJECT_NOTE_ACTION, False, NoteChangeStatus.REJECTED, "거절했습니다"),
    ],
)
async def test_the_owners_button_reaches_core_and_replaces_the_buttons(
    action: str, apply: bool, status: NoteChangeStatus, text: str
) -> None:
    core, slack = FakeCore(), FakeSlack()
    core.note_result = NoteChangeResult(status=status, changed=True, detail="상세")
    await ApprovalActions(core, slack, CONFIG).handle(_press(OWNER, action))
    [(change_id, decision)] = core.note_decisions
    assert (change_id, decision.apply, decision.decided_by) == (CHANGE, apply, OWNER)
    [(_, ts, blocks)] = slack.updates
    assert ts == "1.0"
    assert text in str(blocks)
    assert not [b for b in blocks if b.get("type") == "actions"]


async def test_someone_else_cannot_press() -> None:
    core, slack = FakeCore(), FakeSlack()
    await ApprovalActions(core, slack, CONFIG).handle(_press("U0OTHER", APPLY_NOTE_ACTION))
    assert core.note_decisions == []
