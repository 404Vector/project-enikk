"""Conversions between ORM rows and domain entities.

Rows are parsed back into entities through their validating constructors,
so corrupted rows surface as validation errors instead of bad objects.
"""

from __future__ import annotations

import pydantic_core
from pydantic import TypeAdapter

from enikk_contracts.agents import Execution
from enikk_contracts.approvals import OwnerNoticeBody
from enikk_contracts.common import ConnectorKind, ConversationRef, Engine
from enikk_contracts.outbox import OutboundKind
from enikk_contracts.tasks import TaskStatus
from enikk_core_domain.deployments import DeploymentWatch
from enikk_core_domain.model import (
    Approval,
    ApprovalState,
    Dispatch,
    Lease,
    Notice,
    NoticeStatus,
    OutboxMessage,
    OutboxStatus,
    Session,
    Task,
)
from enikk_persistence_sql.tables import (
    ApprovalRow,
    DeploymentWatchRow,
    DispatchRow,
    NoticeRow,
    OutboxRow,
    SessionRow,
    TaskRow,
)

WATCH_ROW_ID = 1
"""The deployment watch is a single row."""


def session_to_row(session: Session, row: SessionRow | None = None) -> SessionRow:
    row = row or SessionRow(id=session.id)
    row.connector = session.conversation.connector.value
    row.channel = session.conversation.channel
    row.thread = session.conversation.thread
    row.agent = session.agent
    row.engine = session.engine.value
    row.engine_session_id = session.engine_session_id
    row.reply_channel = session.reply_to.channel if session.reply_to else None
    row.reply_thread = session.reply_to.thread if session.reply_to else None
    row.created_at = session.created_at
    row.updated_at = session.updated_at
    return row


def session_from_row(row: SessionRow) -> Session:
    connector = ConnectorKind(row.connector)
    reply_to = None
    if row.reply_channel is not None and row.reply_thread is not None:
        reply_to = ConversationRef(
            connector=connector, channel=row.reply_channel, thread=row.reply_thread
        )
    return Session(
        id=row.id,
        conversation=ConversationRef(connector=connector, channel=row.channel, thread=row.thread),
        agent=row.agent,
        engine=Engine(row.engine),
        engine_session_id=row.engine_session_id,
        reply_to=reply_to,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def task_to_row(task: Task, row: TaskRow | None = None) -> TaskRow:
    row = row or TaskRow(id=task.id)
    row.session_id = task.session_id
    row.source_event_id = task.source_event_id
    row.agent = task.agent
    row.engine = task.engine.value
    row.execution = task.execution.value
    row.input = task.input
    row.status = task.status.value
    row.attempt = task.attempt
    row.max_attempts = task.max_attempts
    row.lease_id = task.lease.id if task.lease else None
    row.lease_expires_at = task.lease.expires_at if task.lease else None
    row.last_lease_id = task.last_lease_id
    row.progress = task.progress
    row.output = task.output
    row.failure = task.failure
    row.created_at = task.created_at
    row.updated_at = task.updated_at
    return row


def task_from_row(row: TaskRow) -> Task:
    lease = None
    if row.lease_id is not None and row.lease_expires_at is not None:
        lease = Lease(id=row.lease_id, expires_at=row.lease_expires_at)
    return Task(
        id=row.id,
        session_id=row.session_id,
        source_event_id=row.source_event_id,
        agent=row.agent,
        engine=Engine(row.engine),
        execution=Execution(row.execution),
        input=row.input,
        status=TaskStatus(row.status),
        attempt=row.attempt,
        max_attempts=row.max_attempts,
        lease=lease,
        last_lease_id=row.last_lease_id,
        progress=row.progress,
        output=row.output,
        failure=row.failure,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


_NOTICE_BODY: TypeAdapter[OwnerNoticeBody] = TypeAdapter(OwnerNoticeBody)


def approval_to_row(approval: Approval, row: ApprovalRow | None = None) -> ApprovalRow:
    row = row or ApprovalRow(id=approval.id)
    row.repository = approval.repository
    row.number = approval.number
    row.head_sha = approval.head_sha
    row.branch = approval.branch
    row.title = approval.title
    row.url = approval.url
    row.state = approval.state.value
    row.created_at = approval.created_at
    row.decided_at = approval.decided_at
    row.decided_by = approval.decided_by
    return row


def approval_from_row(row: ApprovalRow) -> Approval:
    return Approval(
        id=row.id,
        repository=row.repository,
        number=row.number,
        head_sha=row.head_sha,
        branch=row.branch,
        title=row.title,
        url=row.url,
        state=ApprovalState(row.state),
        created_at=row.created_at,
        decided_at=row.decided_at,
        decided_by=row.decided_by,
    )


def deployment_watch_to_row(
    watch: DeploymentWatch, row: DeploymentWatchRow | None = None
) -> DeploymentWatchRow:
    row = row or DeploymentWatchRow(id=WATCH_ROW_ID)
    row.body = watch.model_dump(mode="json")
    return row


def deployment_watch_from_row(row: DeploymentWatchRow) -> DeploymentWatch:
    return DeploymentWatch.model_validate_json(pydantic_core.to_json(row.body))


def notice_to_row(notice: Notice, row: NoticeRow | None = None) -> NoticeRow:
    row = row or NoticeRow(id=notice.id)
    row.body = _NOTICE_BODY.dump_python(notice.body, mode="json")
    row.status = notice.status.value
    row.created_at = notice.created_at
    return row


def notice_from_row(row: NoticeRow) -> Notice:
    return Notice(
        id=row.id,
        body=_NOTICE_BODY.validate_json(pydantic_core.to_json(row.body)),
        status=NoticeStatus(row.status),
        created_at=row.created_at,
    )


def dispatch_to_row(dispatch: Dispatch) -> DispatchRow:
    return DispatchRow(
        id=dispatch.id,
        task_id=dispatch.task_id,
        job_name=dispatch.job_name,
        created_at=dispatch.created_at,
    )


def outbox_to_row(message: OutboxMessage, row: OutboxRow | None = None) -> OutboxRow:
    row = row or OutboxRow(id=message.id)
    row.task_id = message.task_id
    row.source_event_id = message.source_event_id
    row.connector = message.conversation.connector.value
    row.channel = message.conversation.channel
    row.thread = message.conversation.thread
    row.kind = message.kind.value
    row.text = message.text
    row.status = message.status.value
    row.created_at = message.created_at
    row.delivered_at = message.delivered_at
    return row


def outbox_from_row(row: OutboxRow) -> OutboxMessage:
    return OutboxMessage(
        id=row.id,
        task_id=row.task_id,
        source_event_id=row.source_event_id,
        conversation=ConversationRef(
            connector=ConnectorKind(row.connector), channel=row.channel, thread=row.thread
        ),
        kind=OutboundKind(row.kind),
        text=row.text,
        status=OutboxStatus(row.status),
        created_at=row.created_at,
        delivered_at=row.delivered_at,
    )
