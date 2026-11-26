"""In-memory core and Slack for tests."""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from enikk_connector_slack.config import Routes, SlackConfig
from enikk_connector_slack.events import PostedMessage
from enikk_connector_slack.ports import WorkspaceView
from enikk_contracts.approvals import (
    DecisionResult,
    NoticeAcked,
    NoticeBatch,
    ResolutionOutcome,
)
from enikk_contracts.deployments import PullLink, RevertOutcome, RevertRequest, RevertResult
from enikk_contracts.events import EventAccepted
from enikk_contracts.outbox import OutboxAcked, OutboxBatch
from enikk_contracts.prompts import PromotionLinked, PromotionResult, PromotionStatus
from enikk_contracts.tasks import CancelResult
from enikk_contracts.thread_actions import SlackWorkspaceReceived
from enikk_contracts.threads import ReviewThreadLinked
from enikk_core_client import CoreNotFoundError

if TYPE_CHECKING:
    from pydantic import JsonValue

    from enikk_connector_slack.blocks import Message
    from enikk_connector_slack.events import MessageRef
    from enikk_contracts.approvals import DecisionRequest, NoticeAck, NoticePoll, OwnerNotice
    from enikk_contracts.events import InboundEvent
    from enikk_contracts.outbox import OutboundMessage, OutboxAck, OutboxFile, OutboxPoll
    from enikk_contracts.prompts import PromotionDecision, PromotionPosted
    from enikk_contracts.thread_actions import SlackWorkspaceReport
    from enikk_contracts.threads import ReviewThreadOpened

OWNER = "U0OWNER"
BOT = "U0BOT"
CONFIG = SlackConfig(owner=OWNER, routes=Routes(direct_message="assistant", mention="helper"))


class FakeCore:
    def __init__(self) -> None:
        self.events: list[InboundEvent] = []
        self.pending: list[OutboundMessage] = []
        self.acked: list[uuid.UUID] = []
        self.fail_with: Exception | None = None
        self.sessions: set[str] = set()
        self.notices: list[OwnerNotice] = []
        self.notices_acked: list[uuid.UUID] = []
        self.acks: list[NoticeAck] = []
        self.decisions: list[tuple[uuid.UUID, DecisionRequest]] = []
        self.decision_result = DecisionResult(outcome=ResolutionOutcome.MERGED)
        self.reverts: list[RevertRequest] = []
        self.revert_result = RevertResult(
            outcome=RevertOutcome.OPENED,
            pull=PullLink(number=60, title="Revert", url="https://github.com/x/y/pull/60"),
        )
        self.reviews: list[ReviewThreadOpened] = []
        self.review_linked = ReviewThreadLinked()
        self.review_fails: Exception | None = None
        self.promotion_decisions: list[tuple[uuid.UUID, PromotionDecision]] = []
        self.promotion_result = PromotionResult(
            status=PromotionStatus.PROMOTED,
            changed=True,
            prompt="assistant",
            production_version=3,
            candidate_version=5,
            detail="반영: assistant 역할은 최대 60초 안에",
        )
        self.promotions_posted: list[tuple[uuid.UUID, PromotionPosted]] = []
        self.cancels: list[uuid.UUID] = []
        self.cancel_finds_nothing = False
        self.files: dict[uuid.UUID, OutboxFile] = {}
        self.workspace_reports: list[SlackWorkspaceReport] = []

    async def outbox_file(self, message_id: uuid.UUID) -> OutboxFile:
        found = self.files.get(message_id)
        if found is None:
            raise CoreNotFoundError("no file")
        return found

    async def submit_event(self, event: InboundEvent) -> EventAccepted:
        if self.fail_with is not None:
            raise self.fail_with
        thread = event.conversation.thread
        if event.continue_only and thread not in self.sessions:
            raise CoreNotFoundError("no conversation")
        self.sessions.add(thread)
        duplicate = any(e.event_id == event.event_id for e in self.events)
        if not duplicate:
            self.events.append(event)
        return EventAccepted(
            task_id=uuid.uuid4(), session_id=uuid.uuid4(), agent=event.agent, duplicate=duplicate
        )

    async def poll_outbox(self, poll: OutboxPoll) -> OutboxBatch:
        return OutboxBatch(messages=tuple(self.pending[: poll.limit]))

    async def ack_outbox(self, ack: OutboxAck) -> OutboxAcked:
        self.acked.extend(ack.message_ids)
        self.pending = [m for m in self.pending if m.message_id not in ack.message_ids]
        return OutboxAcked(acknowledged=len(ack.message_ids))

    async def poll_notices(self, poll: NoticePoll) -> NoticeBatch:
        return NoticeBatch(notices=tuple(self.notices[: poll.limit]))

    async def ack_notices(self, ack: NoticeAck) -> NoticeAcked:
        self.acks.append(ack)
        self.notices_acked.extend(ack.notice_ids)
        self.notices = [n for n in self.notices if n.notice_id not in ack.notice_ids]
        return NoticeAcked(acknowledged=len(ack.notice_ids))

    async def decide(self, approval_id: uuid.UUID, request: DecisionRequest) -> DecisionResult:
        if self.fail_with is not None:
            raise self.fail_with
        self.decisions.append((approval_id, request))
        return self.decision_result

    async def request_revert(self, request: RevertRequest) -> RevertResult:
        if self.fail_with is not None:
            raise self.fail_with
        self.reverts.append(request)
        return self.revert_result

    async def review_thread_opened(self, review: ReviewThreadOpened) -> ReviewThreadLinked:
        if self.review_fails is not None:
            raise self.review_fails
        self.reviews.append(review)
        return self.review_linked

    async def decide_promotion(
        self, promotion_id: uuid.UUID, decision: PromotionDecision
    ) -> PromotionResult:
        if self.fail_with is not None:
            raise self.fail_with
        self.promotion_decisions.append((promotion_id, decision))
        return self.promotion_result

    async def promotion_posted(
        self, promotion_id: uuid.UUID, posted: PromotionPosted
    ) -> PromotionLinked:
        self.promotions_posted.append((promotion_id, posted))
        return PromotionLinked(told_origin=True)

    async def report_slack_workspace(self, report: SlackWorkspaceReport) -> SlackWorkspaceReceived:
        self.workspace_reports.append(report)
        return SlackWorkspaceReceived(emoji=None if report.emoji is None else len(report.emoji))

    async def cancel_task(self, task_id: uuid.UUID) -> CancelResult:
        if self.fail_with is not None:
            raise self.fail_with
        self.cancels.append(task_id)
        cancelled = () if self.cancel_finds_nothing else (task_id,)
        return CancelResult(task_id=task_id, cancelled=cancelled)


class FakeSlack:
    def __init__(self) -> None:
        self.posts: list[tuple[str, str | None, str]] = []
        self.view = WorkspaceView(url="https://enikk.slack.test/", emoji=("e-done",))
        self.fail_workspace: list[Exception] = []
        self.reactions: list[tuple[str, str, str]] = []
        self.fail_posts: list[Exception] = []
        self.messages: list[tuple[str, Message]] = []
        self.replies: list[tuple[str, str, Message]] = []
        self.fail_replies: list[Exception] = []
        self.uploads: list[tuple[str, str, str, str]] = []
        self.files: list[tuple[str, str, str, bytes, str]] = []
        self.fail_files: list[Exception] = []
        self.updates: list[tuple[str, str, list[dict[str, JsonValue]]]] = []
        self.no_permalinks = False
        self.ephemerals: list[tuple[str, str, str]] = []
        self.deleted: list[tuple[str, str]] = []
        self.fail_channels: dict[str, Exception] = {}
        self.channel_ids: dict[str, str] = {}
        self.unknown_emoji: set[str] = set()

    async def channel_id(self, name: str) -> str | None:
        return self.channel_ids.get(name)

    async def workspace(self) -> WorkspaceView:
        if self.fail_workspace:
            raise self.fail_workspace.pop(0)
        return self.view

    async def post_reply(self, channel: str, thread: str | None, text: str) -> None:
        if self.fail_posts:
            raise self.fail_posts.pop(0)
        self.posts.append((channel, thread, text))

    async def post_message(
        self, channel: str, message: Message, thread: str | None = None
    ) -> PostedMessage:
        if thread is not None:
            if self.fail_replies:
                raise self.fail_replies.pop(0)
            self.replies.append((channel, thread, message))
            return PostedMessage(channel=channel, ts=f"{thread}.{len(self.replies)}")
        if channel in self.fail_channels:
            raise self.fail_channels[channel]
        if self.fail_posts:
            raise self.fail_posts.pop(0)
        self.messages.append((channel, message))
        posted = "D0OWNER" if channel == OWNER else channel
        return PostedMessage(channel=posted, ts=f"{len(self.messages)}.0")

    async def upload_text(
        self, channel: str, thread: str, filename: str, content: str, title: str
    ) -> None:
        self.uploads.append((channel, thread, filename, content))

    async def upload_file(
        self, channel: str, thread: str, filename: str, content: bytes, title: str
    ) -> None:
        if self.fail_files:
            raise self.fail_files.pop(0)
        self.files.append((channel, thread, filename, content, title))

    async def update_message(
        self, channel: str, ts: str, text: str, blocks: list[dict[str, JsonValue]]
    ) -> None:
        self.updates.append((channel, ts, blocks))

    async def delete_message(self, channel: str, ts: str) -> None:
        self.deleted.append((channel, ts))

    async def post_ephemeral(self, channel: str, user: str, text: str) -> None:
        self.ephemerals.append((channel, user, text))

    async def permalink(self, channel: str, ts: str) -> str | None:
        if self.no_permalinks:
            return None
        return f"https://slack.test/archives/{channel}/p{ts.replace('.', '')}"

    async def react(self, message: MessageRef, emoji: str) -> bool:
        if emoji in self.unknown_emoji:
            return False
        self.reactions.append(("+", message.ts, emoji))
        return True

    async def unreact(self, message: MessageRef, emoji: str) -> None:
        self.reactions.append(("-", message.ts, emoji))
