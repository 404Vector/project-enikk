"""The owner's DM: approval requests out, button presses in (ADR-0020)."""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import TYPE_CHECKING, Final

from pydantic import ValidationError

from enikk_connector_slack import blocks
from enikk_connector_slack.events import BlockActions
from enikk_connector_slack.ports import SlackDeliveryError
from enikk_contracts.approvals import (
    ApprovalRequested,
    ApprovalResolved,
    ChecksFailed,
    Decision,
    DecisionRequest,
    NoticeAck,
    NoticePoll,
)
from enikk_contracts.deployments import BuildFailed, DeploymentFinished
from enikk_core_client import CoreApiError, CoreUnavailableError

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from enikk_connector_slack.config import SlackConfig
    from enikk_connector_slack.outbound import RelayPolicy
    from enikk_connector_slack.ports import CoreApi, SlackApi
    from enikk_contracts.approvals import OwnerNotice

    Sleep = Callable[[float], Awaitable[None]]

DECISIONS: Final = {blocks.APPROVE_ACTION: Decision.APPROVE, blocks.REJECT_ACTION: Decision.REJECT}

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
        card = body.pull if isinstance(body, ApprovalRequested | ChecksFailed) else None
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
        try:
            posted = await self._slack.post_message(self._owner, message)
        except SlackDeliveryError as exc:
            if exc.transient:
                raise
            _log.error("dropped owner notice %s: %s", notice.notice_id, exc)
            return
        if card is not None and card.diff:
            name = f"pr-{card.number}-{card.head_sha[:7]}.diff"
            note = " (잘림)" if card.diff_truncated else ""
            await self._slack.upload_text(
                posted.channel, posted.ts, name, card.diff, f"#{card.number} diff{note}"
            )


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
            return
        for action in actions.actions:
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
