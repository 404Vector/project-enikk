from __future__ import annotations

import uuid

import pytest

from enikk_connector_slack.approvals import NoticeRelay
from enikk_connector_slack.blocks import Actions
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
    for _, message in slack.messages:
        assert not any(isinstance(b, Actions) for b in message.blocks)


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
