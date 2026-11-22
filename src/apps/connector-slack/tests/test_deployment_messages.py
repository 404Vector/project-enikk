from __future__ import annotations

import uuid

import pytest

from enikk_connector_slack.approvals import ApprovalActions, NoticeRelay
from enikk_connector_slack.blocks import REVERT_ACTION, Actions
from enikk_connector_slack.outbound import RelayPolicy
from enikk_connector_slack.testing import CONFIG, OWNER, FakeCore, FakeSlack
from enikk_contracts.approvals import OwnerNotice
from enikk_contracts.deployments import (
    AppDeployment,
    AppResult,
    BuildFailed,
    DeploymentFinished,
    PullLink,
)

pytestmark = pytest.mark.anyio

POLICY = RelayPolicy(wait_seconds=0, batch=10, backoff_max_seconds=1)
SHA = "f" * 40
PULL = PullLink(number=40, title="feat: <new> thing", url="https://github.com/x/y/pull/40")


async def _relay(*bodies: DeploymentFinished | BuildFailed) -> FakeSlack:
    core, slack = FakeCore(), FakeSlack()
    core.notices += [OwnerNotice(notice_id=uuid.uuid4(), body=b) for b in bodies]
    assert await NoticeRelay(core, slack, CONFIG, POLICY).relay_once() == len(bodies)
    assert slack.uploads == []
    return slack


async def test_deployment_results_go_to_the_owner_without_buttons() -> None:
    ok = DeploymentFinished(
        repository="404Vector/project-enikk",
        commit=SHA,
        pull=PULL,
        succeeded=True,
        apps=(AppDeployment(app="enikk-core", result=AppResult.SUCCEEDED),),
    )
    failed = ok.model_copy(
        update={
            "succeeded": False,
            "apps": (
                AppDeployment(
                    app="enikk-core", result=AppResult.FAILED, detail="PostSync hook failed"
                ),
            ),
        }
    )
    slack = await _relay(ok, failed)
    [(channel, first), (_, second)] = slack.messages
    assert channel == OWNER
    assert first.text.startswith("배포 성공")
    assert "feat: &lt;new&gt; thing" in str(first.payload())
    assert second.text.startswith("배포 실패")
    assert "PostSync hook failed" in str(second.payload())
    assert "이전 버전" in str(second.payload())
    assert not any(isinstance(b, Actions) for b in first.blocks)
    [revert] = [b for b in second.blocks if isinstance(b, Actions)]
    assert [(e.action_id, e.value) for e in revert.elements] == [(REVERT_ACTION, SHA)]
    assert revert.elements[0].confirm is not None


async def test_rollback_results_have_no_revert_button() -> None:
    rollback = DeploymentFinished(
        repository="404Vector/project-enikk",
        commit=SHA,
        succeeded=False,
        rollback_of="1" * 40,
        apps=(AppDeployment(app="enikk-core", result=AppResult.FAILED),),
    )
    slack = await _relay(rollback)
    [(_, message)] = slack.messages
    assert message.text.startswith("롤백 실패")
    assert not any(isinstance(b, Actions) for b in message.blocks)


async def test_revert_button_asks_core_and_replaces_the_button() -> None:
    core, slack = FakeCore(), FakeSlack()
    press = {
        "type": "block_actions",
        "user": {"id": OWNER},
        "container": {"type": "message", "channel_id": "D0OWNER", "message_ts": "2.0"},
        "message": {
            "text": "배포 실패",
            "blocks": [
                {"type": "section", "text": {"type": "mrkdwn", "text": "x"}},
                {"type": "actions", "elements": []},
            ],
        },
        "actions": [{"action_id": REVERT_ACTION, "value": SHA}],
    }
    await ApprovalActions(core, slack, CONFIG).handle(press)
    [request] = core.reverts
    assert (request.commit, request.requested_by) == (SHA, OWNER)
    [(channel, ts, updated)] = slack.updates
    assert (channel, ts) == ("D0OWNER", "2.0")
    assert [b["type"] for b in updated] == ["section", "context"]
    assert "pull/60" in str(updated[-1])


async def test_build_failure_links_the_run() -> None:
    slack = await _relay(
        BuildFailed(
            repository="404Vector/project-enikk",
            commit=SHA,
            pull=None,
            run_url="https://github.com/x/y/actions/runs/9",
            conclusion="failure",
        )
    )
    [(_, message)] = slack.messages
    assert message.text == f"이미지 빌드 실패: {SHA[:7]}"
    assert "actions/runs/9" in str(message.payload())
