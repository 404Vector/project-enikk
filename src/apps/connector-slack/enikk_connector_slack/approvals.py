"""The owner's DM: approval and prompt promotion requests out, button presses in
(ADR-0020, ADR-0032)."""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import TYPE_CHECKING, Final

from pydantic import ValidationError

from enikk_connector_slack import blocks
from enikk_connector_slack.events import BlockActions, PostedMessage
from enikk_connector_slack.ports import SlackDeliveryError
from enikk_contracts.approvals import (
    ApprovalOutdated,
    ApprovalRequested,
    ApprovalResolved,
    ChecksFailed,
    Decision,
    DecisionRequest,
    NoticeAck,
    NoticePoll,
)
from enikk_contracts.deployments import BuildFailed, DeploymentFinished, RevertRequest
from enikk_contracts.prompts import (
    PromotionAction,
    PromotionDecision,
    PromotionPosted,
    PromotionRequested,
)
from enikk_contracts.threads import ReviewThreadLinked, ReviewThreadOpened
from enikk_core_client import CoreApiError, CoreUnavailableError

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from enikk_connector_slack.config import SlackConfig
    from enikk_connector_slack.outbound import RelayPolicy
    from enikk_connector_slack.ports import CoreApi, SlackApi
    from enikk_contracts.approvals import OwnerNotice, PullRequestCard

    Sleep = Callable[[float], Awaitable[None]]

DECISIONS: Final = {blocks.APPROVE_ACTION: Decision.APPROVE, blocks.REJECT_ACTION: Decision.REJECT}
PROMOTIONS: Final = {
    blocks.PROMOTE_PROMPT_ACTION: PromotionAction.PROMOTE,
    blocks.REJECT_PROMPT_ACTION: PromotionAction.REJECT,
    blocks.REVERT_PROMPT_ACTION: PromotionAction.REVERT,
}

_log = logging.getLogger(__name__)


class NoticeRelay:
    """Posts each owner notice to the owner's DM, then acknowledges it to core."""

    def __init__(
        self,
        core: CoreApi,
        slack: SlackApi,
        config: SlackConfig,
        policy: RelayPolicy,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        self._core = core
        self._slack = slack
        self._owner = config.owner
        self._deploy_channel = config.deploy_notify_channel
        self._policy = policy
        self._sleep = sleep

    async def run(self, stop: asyncio.Event) -> None:
        delay = 1.0
        while not stop.is_set():
            try:
                await self.relay_once()
            except (CoreUnavailableError, SlackDeliveryError) as exc:
                _log.warning("notice relay paused for %.0fs: %s", delay, exc)
                await self._sleep(delay)
                delay = min(delay * 2, self._policy.backoff_max_seconds)
            else:
                delay = 1.0

    async def relay_once(self) -> int:
        poll = NoticePoll(wait_seconds=self._policy.wait_seconds, limit=10)
        batch = await self._core.poll_notices(poll)
        settled: list[uuid.UUID] = []
        try:
            for notice in batch.notices:
                await self._deliver(notice)
                settled.append(notice.notice_id)
        finally:
            if settled:
                await self._core.ack_notices(NoticeAck(notice_ids=tuple(settled)))
        return len(settled)

    async def _deliver(self, notice: OwnerNotice) -> None:
        body = notice.body
        if isinstance(body, PromotionRequested):
            await self._deliver_promotion(notice, body)
            return
        if isinstance(body, ApprovalOutdated):
            message = blocks.outdated(body)
            await self._slack.update_message(
                body.replaces.channel, body.replaces.ts, message.text, message.json_blocks()
            )
            return
        card = body.pull if isinstance(body, ApprovalRequested | ChecksFailed) else None
        replaces = body.replaces if isinstance(body, ApprovalRequested | ChecksFailed) else None
        if isinstance(body, ApprovalRequested):
            message = blocks.approval_request(body)
        elif isinstance(body, ChecksFailed):
            message = blocks.checks_failed(body)
        elif isinstance(body, ApprovalResolved):
            message = blocks.resolved(body)
        elif isinstance(body, DeploymentFinished):
            message = blocks.deployment_finished(body)
        elif isinstance(body, BuildFailed):
            message = blocks.build_failed(body)
        if isinstance(body, DeploymentFinished | BuildFailed):
            await self._deliver_deployment(notice, message)
            return
        if replaces is not None and card is not None:
            # One message per pull request: a new commit updates it in place.
            await self._slack.update_message(
                replaces.channel, replaces.ts, message.text, message.json_blocks()
            )
            posted = PostedMessage(channel=replaces.channel, ts=replaces.ts)
            await self._post_text(
                posted,
                card,
                f":arrows_counterclockwise: 새 커밋 `{card.head_sha[:7]}` 로 갱신했습니다. "
                "아래는 새 커밋의 상세입니다.",
            )
        else:
            try:
                posted = await self._slack.post_message(self._owner, message)
            except SlackDeliveryError as exc:
                if exc.transient:
                    raise
                _log.error("dropped owner notice %s: %s", notice.notice_id, exc)
                return
        if card is None:
            return
        updated = replaces is not None
        linked = await self._link_threads(posted, card, updated=updated)
        await self._post_details(posted.channel, posted.ts, card)
        if linked.report is not None and not updated:
            await self._post_text(posted, card, f"*개발 역할 보고*\n{linked.report}")
        if card.diff:
            name = f"pr-{card.number}-{card.head_sha[:7]}.diff"
            note = " (잘림)" if card.diff_truncated else ""
            await self._slack.upload_text(
                posted.channel, posted.ts, name, card.diff, f"#{card.number} diff{note}"
            )

    async def _deliver_promotion(self, notice: OwnerNotice, body: PromotionRequested) -> None:
        """The request in the owner's DM, the diff in its thread, then the asking thread
        hears where it is (ADR-0032)."""
        try:
            posted = await self._slack.post_message(self._owner, blocks.promotion_request(body))
        except SlackDeliveryError as exc:
            if exc.transient:
                raise
            _log.error("dropped prompt promotion notice %s: %s", notice.notice_id, exc)
            return
        if body.origin is not None:
            origin = await self._slack.permalink(body.origin.channel, body.origin.thread)
            if origin is not None:
                await self._reply(posted, f"원래 요청 스레드: {origin}")
        for reply in blocks.promotion_details(body):
            try:
                await self._slack.post_message(posted.channel, reply, thread=posted.ts)
            except SlackDeliveryError as exc:
                _log.warning("could not post the diff of %s: %s", body.prompt, exc)
                break
        link = await self._slack.permalink(posted.channel, posted.ts)
        try:
            await self._core.promotion_posted(body.promotion_id, PromotionPosted(link=link))
        except (CoreApiError, CoreUnavailableError) as exc:
            _log.warning("could not tell the asking thread about %s: %s", body.prompt, exc)

    async def _reply(self, posted: PostedMessage, text: str) -> None:
        try:
            await self._slack.post_reply(posted.channel, posted.ts, text)
        except SlackDeliveryError as exc:
            _log.warning("could not reply in %s: %s", posted.channel, exc)

    async def _deliver_deployment(self, notice: OwnerNotice, message: blocks.Message) -> None:
        """Post in the deployment channel, or in the owner's DM if that fails (ADR-0026)."""
        try:
            await self._slack.post_message(self._deploy_channel, message)
        except SlackDeliveryError as exc:
            _log.error(
                "could not post notice %s in %s, sending it to the owner's DM: %s",
                notice.notice_id,
                self._deploy_channel,
                exc,
            )
        else:
            return
        try:
            await self._slack.post_message(
                self._owner, blocks.sent_to_dm_instead(message, self._deploy_channel)
            )
        except SlackDeliveryError as exc:
            if exc.transient:
                raise
            _log.error("dropped owner notice %s: %s", notice.notice_id, exc)

    async def _link_threads(
        self, posted: PostedMessage, pull: PullRequestCard, *, updated: bool = False
    ) -> ReviewThreadLinked:
        """Exchange links between this review thread and the thread that asked for the PR,
        and tell core which message shows the PR so the next commit updates it."""
        review = ReviewThreadOpened(
            repository=pull.repository,
            number=pull.number,
            branch=pull.branch,
            review_link=await self._slack.permalink(posted.channel, posted.ts),
            channel=posted.channel,
            ts=posted.ts,
            updated=updated,
        )
        try:
            linked = await self._core.review_thread_opened(review)
        except (CoreApiError, CoreUnavailableError) as exc:
            _log.warning("could not link the review thread of PR #%d: %s", pull.number, exc)
            return ReviewThreadLinked()
        if linked.origin is not None and not updated:
            origin = await self._slack.permalink(linked.origin.channel, linked.origin.thread)
            if origin is not None:
                await self._post_text(posted, pull, f"원래 요청 스레드: {origin}")
        return linked

    async def _post_text(self, posted: PostedMessage, pull: PullRequestCard, text: str) -> None:
        try:
            await self._slack.post_reply(posted.channel, posted.ts, text)
        except SlackDeliveryError as exc:
            _log.warning("could not post in the review thread of PR #%d: %s", pull.number, exc)

    async def _post_details(self, channel: str, thread: str, pull: PullRequestCard) -> None:
        for reply in blocks.details(pull):
            try:
                await self._slack.post_message(channel, reply, thread=thread)
            except SlackDeliveryError as exc:
                _log.warning("could not post details of PR #%d: %s", pull.number, exc)
                return


class ApprovalActions:
    """Handles the owner's button presses. Anyone else is ignored (I-01)."""

    def __init__(self, core: CoreApi, slack: SlackApi, config: SlackConfig) -> None:
        self._core = core
        self._slack = slack
        self._owner = config.owner

    async def handle(self, payload: object) -> None:
        try:
            actions = BlockActions.model_validate(payload)
        except ValidationError:
            return
        if actions.user.id != self._owner:
            _log.warning("ignored a button press by %s", actions.user.id)
            await self._slack.post_ephemeral(
                actions.channel_id, actions.user.id, blocks.NOT_OWNER_TEXT
            )
            return
        for action in actions.actions:
            if action.action_id == blocks.REVERT_ACTION:
                await self._revert(actions, action.value)
                continue
            promotion = PROMOTIONS.get(action.action_id)
            if promotion is not None:
                await self._promote(actions, action.value, promotion)
                continue
            decision = DECISIONS.get(action.action_id)
            if decision is None:
                continue
            try:
                approval_id = uuid.UUID(action.value)
            except ValueError:
                continue
            await self._decide(actions, approval_id, decision)

    async def _decide(
        self, actions: BlockActions, approval_id: uuid.UUID, decision: Decision
    ) -> None:
        request = DecisionRequest(decision=decision, decided_by=actions.user.id)
        try:
            result = await self._core.decide(approval_id, request)
        except (CoreApiError, CoreUnavailableError) as exc:
            _log.warning("decision on %s failed: %s", approval_id, exc)
            await self._slack.post_reply(
                actions.channel_id,
                actions.message_ts,
                f":warning: 처리하지 못했습니다. 잠시 뒤 다시 눌러 주세요. ({exc})",
            )
            return
        kept = [b for b in actions.message_blocks if b.get("type") != "actions"]
        footer = blocks.decided(result.outcome, result.detail, actions.user.id)
        await self._slack.update_message(
            actions.channel_id,
            actions.message_ts,
            actions.message_text,
            [*kept, footer.model_dump()],
        )

    async def _promote(self, actions: BlockActions, value: str, action: PromotionAction) -> None:
        try:
            promotion_id = uuid.UUID(value)
        except ValueError:
            return
        decision = PromotionDecision(action=action, decided_by=actions.user.id)
        try:
            result = await self._core.decide_promotion(promotion_id, decision)
        except (CoreApiError, CoreUnavailableError) as exc:
            _log.warning("prompt %s on %s failed: %s", action, promotion_id, exc)
            await self._slack.post_reply(
                actions.channel_id,
                actions.message_ts,
                f":warning: 처리하지 못했습니다. 아무것도 바뀌지 않았습니다. "
                f"잠시 뒤 다시 눌러 주세요. ({exc})",
            )
            return
        kept = [b for b in actions.message_blocks if b.get("type") != "actions"]
        footer = blocks.promotion_decided(str(promotion_id), result, actions.user.id)
        await self._slack.update_message(
            actions.channel_id,
            actions.message_ts,
            actions.message_text,
            [*kept, *(b.model_dump(mode="json", exclude_none=True) for b in footer)],
        )

    async def _revert(self, actions: BlockActions, commit: str) -> None:
        try:
            request = RevertRequest(commit=commit, requested_by=actions.user.id)
        except ValidationError:
            return
        try:
            result = await self._core.request_revert(request)
        except (CoreApiError, CoreUnavailableError) as exc:
            _log.warning("revert of %s failed: %s", commit[:7], exc)
            await self._slack.post_reply(
                actions.channel_id,
                actions.message_ts,
                f":warning: 되돌리기를 요청하지 못했습니다. 잠시 뒤 다시 눌러 주세요. ({exc})",
            )
            return
        kept = [b for b in actions.message_blocks if b.get("type") != "actions"]
        footer = blocks.revert_requested(result, actions.user.id)
        await self._slack.update_message(
            actions.channel_id,
            actions.message_ts,
            actions.message_text,
            [*kept, footer.model_dump()],
        )
