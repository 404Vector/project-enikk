from __future__ import annotations

import logging
import uuid
from pathlib import Path

import pytest

from enikk_connector_slack.approvals import ApprovalActions, NoticeRelay
from enikk_connector_slack.blocks import NOT_OWNER_TEXT, REVERT_ACTION, Actions, Context
from enikk_connector_slack.config import DEFAULT_DEPLOY_NOTIFY_CHANNEL, SlackConfig
from enikk_connector_slack.outbound import RelayPolicy
from enikk_connector_slack.ports import SlackDeliveryError
from enikk_connector_slack.testing import CONFIG, OWNER, FakeCore, FakeSlack
from enikk_contracts.approvals import OwnerNotice
from enikk_contracts.deployments import (
    AppDeployment,
    AppResult,
    BuildFailed,
    DeploymentFinished,
    PullLink,
)
from enikk_kernel.errors import ConfigurationError

pytestmark = pytest.mark.anyio

POLICY = RelayPolicy(wait_seconds=0, batch=10, backoff_max_seconds=1)
SHA = "f" * 40
PULL = PullLink(number=40, title="feat: <new> thing", url="https://github.com/x/y/pull/40")
RELEASE = CONFIG.deploy_notify_channel
SLACK_YAML = Path(__file__).resolve().parents[4] / "config" / "slack.yaml"
FAILED_BUILD = BuildFailed(
    repository="404Vector/project-enikk",
    commit=SHA,
    run_url="https://github.com/x/y/actions/runs/9",
    conclusion="failure",
)


async def _relay(
    *bodies: DeploymentFinished | BuildFailed, slack: FakeSlack | None = None
) -> FakeSlack:
    core, slack = FakeCore(), slack or FakeSlack()
    core.notices += [OwnerNotice(notice_id=uuid.uuid4(), body=b) for b in bodies]
    assert await NoticeRelay(core, slack, CONFIG, POLICY).relay_once() == len(bodies)
    assert core.notices_acked
    assert slack.uploads == []
    return slack


def _revert_press(user: str, channel: str) -> dict[str, object]:
    return {
        "type": "block_actions",
        "user": {"id": user},
        "container": {"type": "message", "channel_id": channel, "message_ts": "2.0"},
        "message": {
            "text": "배포 실패",
            "blocks": [
                {"type": "section", "text": {"type": "mrkdwn", "text": "x"}},
                {"type": "actions", "elements": []},
            ],
        },
        "actions": [{"action_id": REVERT_ACTION, "value": SHA}],
    }


def test_the_deployment_channel_defaults_to_release_and_can_be_set() -> None:
    assert DEFAULT_DEPLOY_NOTIFY_CHANNEL == "C0CKT1ZNZ43"
    assert SlackConfig.load(SLACK_YAML).deploy_notify_channel == "C0CKT1ZNZ43"
    bare = SlackConfig.parse("owner: U0OWNER\nroutes: {direct_message: a, mention: a}\n")
    assert bare.deploy_notify_channel == "C0CKT1ZNZ43"
    assert bare.with_deploy_channel(None) is bare
    assert bare.with_deploy_channel("C0OTHER").deploy_notify_channel == "C0OTHER"
    with pytest.raises(ConfigurationError):
        bare.with_deploy_channel("#release")


async def test_notices_go_to_the_configured_channel() -> None:
    core, slack = FakeCore(), FakeSlack()
    config = CONFIG.with_deploy_channel("C0OTHER")
    core.notices.append(OwnerNotice(notice_id=uuid.uuid4(), body=FAILED_BUILD))
    await NoticeRelay(core, slack, config, POLICY).relay_once()
    assert [channel for channel, _ in slack.messages] == ["C0OTHER"]


@pytest.mark.parametrize(
    "error",
    [
        SlackDeliveryError("chat.postMessage: not_in_channel", transient=False),
        SlackDeliveryError("chat.postMessage: channel_not_found", transient=False),
        SlackDeliveryError("chat.postMessage: ClientError()", transient=True),
    ],
)
async def test_a_channel_failure_is_logged_and_sent_to_the_owner_dm(
    error: SlackDeliveryError, caplog: pytest.LogCaptureFixture
) -> None:
    slack = FakeSlack()
    slack.fail_channels[RELEASE] = error
    with caplog.at_level(logging.ERROR):
        await _relay(FAILED_BUILD, slack=slack)
    [(channel, message)] = slack.messages
    assert channel == OWNER
    assert isinstance(message.blocks[0], Context)
    assert RELEASE in str(message.blocks[0].model_dump())
    [revert] = [b for b in message.blocks if isinstance(b, Actions)]
    assert revert.elements[0].action_id == REVERT_ACTION
    assert f"in {RELEASE}, sending it to the owner's DM" in caplog.text


async def test_a_failed_dm_fallback_is_retried_when_transient() -> None:
    core, slack = FakeCore(), FakeSlack()
    slack.fail_channels[RELEASE] = SlackDeliveryError("x: not_in_channel", transient=False)
    slack.fail_channels[OWNER] = SlackDeliveryError("x: ratelimited", transient=True)
    core.notices.append(OwnerNotice(notice_id=uuid.uuid4(), body=FAILED_BUILD))
    with pytest.raises(SlackDeliveryError):
        await NoticeRelay(core, slack, CONFIG, POLICY).relay_once()
    assert core.notices_acked == []


@pytest.mark.parametrize("channel", [RELEASE, "D0OWNER"])
async def test_only_the_owner_can_revert_from_the_channel_or_the_dm(channel: str) -> None:
    core, slack = FakeCore(), FakeSlack()
    await ApprovalActions(core, slack, CONFIG).handle(_revert_press("U0SOMEONE", channel))
    assert core.reverts == []
    assert slack.updates == []
    assert slack.ephemerals == [(channel, "U0SOMEONE", NOT_OWNER_TEXT)]

    await ApprovalActions(core, slack, CONFIG).handle(_revert_press(OWNER, channel))
    [request] = core.reverts
    assert request.requested_by == OWNER
    [(updated_channel, _, _)] = slack.updates
    assert updated_channel == channel


async def test_deployment_results_go_to_the_channel() -> None:
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
    assert channel == RELEASE
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
    [(channel, message)] = slack.messages
    assert channel == RELEASE
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
    slack = await _relay(FAILED_BUILD)
    [(channel, message)] = slack.messages
    assert channel == RELEASE
    assert message.text == f"이미지 빌드 실패: {SHA[:7]}"
    assert "actions/runs/9" in str(message.payload())


async def test_a_failed_conversation_summary_is_one_line_in_the_owners_dm() -> None:
    from datetime import date

    from enikk_contracts.digests import DigestFailed

    core, slack = FakeCore(), FakeSlack()
    body = DigestFailed(day=date(2026, 11, 23), reason="prompt <missing>", retry=True)
    core.notices.append(OwnerNotice(notice_id=uuid.uuid4(), body=body))
    assert await NoticeRelay(core, slack, CONFIG, POLICY).relay_once() == 1
    [(channel, message)] = slack.messages
    assert channel == OWNER
    rendered = str(message.payload())
    assert "2026-11-23 대화 요약을 만들지 못했습니다" in rendered
    assert "prompt &lt;missing&gt;" in rendered
    assert "다음 날 실행 때 한 번 더 시도합니다" in rendered
    assert "\n" not in message.payload()[0]["text"]["text"]  # type: ignore[index]


async def test_a_schedule_posts_to_any_channel_the_bot_is_in() -> None:
    """ADR-0039 S6: no allowlist; inviting the bot is the owner's permission."""
    from enikk_contracts.digests import ScheduleDelivered

    core, slack = FakeCore(), FakeSlack()
    slack.channel_ids["general"] = "C0GENERAL"
    body = ScheduleDelivered(name="economy", text="오늘의 경제", channel="general")
    core.notices.append(OwnerNotice(notice_id=uuid.uuid4(), body=body))
    assert await NoticeRelay(core, slack, CONFIG, POLICY).relay_once() == 1
    [(channel, posted)] = slack.messages
    assert channel == "C0GENERAL"
    assert "오늘의 경제" in str(posted.payload())


async def test_a_channel_the_bot_cannot_see_goes_to_the_dm_with_what_to_do() -> None:
    from enikk_contracts.digests import ScheduleDelivered

    core, slack = FakeCore(), FakeSlack()
    body = ScheduleDelivered(name="economy", text="오늘의 경제", channel="general")
    core.notices.append(OwnerNotice(notice_id=uuid.uuid4(), body=body))
    assert await NoticeRelay(core, slack, CONFIG, POLICY).relay_once() == 1
    [(channel, message)] = slack.messages
    assert channel == OWNER
    assert "/invite @enikk" in str(message.payload())


async def test_a_channel_the_bot_is_not_in_goes_to_the_dm_with_what_to_do() -> None:
    from enikk_contracts.digests import ScheduleDelivered

    core, slack = FakeCore(), FakeSlack()
    slack.fail_channels["C04CF12FF2Q"] = SlackDeliveryError("x: not_in_channel", transient=False)
    body = ScheduleDelivered(name="economy", text="오늘의 경제", channel="C04CF12FF2Q")
    core.notices.append(OwnerNotice(notice_id=uuid.uuid4(), body=body))
    assert await NoticeRelay(core, slack, CONFIG, POLICY).relay_once() == 1
    [(_, message)] = [m for m in slack.messages if m[0] == OWNER]
    assert "/invite @enikk" in str(message.payload())
    assert "오늘의 경제" in str(message.payload())


async def test_a_schedule_channel_may_be_given_by_its_id() -> None:
    from enikk_contracts.digests import ScheduleDelivered

    core, slack = FakeCore(), FakeSlack()
    body = ScheduleDelivered(name="economy", text="오늘의 경제", channel="C04CF12FF2Q")
    core.notices.append(OwnerNotice(notice_id=uuid.uuid4(), body=body))
    assert await NoticeRelay(core, slack, CONFIG, POLICY).relay_once() == 1
    [(channel, _)] = slack.messages
    assert channel == "C04CF12FF2Q"
