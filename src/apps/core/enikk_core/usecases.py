"""Core use cases. Each runs in one unit of work and returns a contract model."""

from __future__ import annotations

import base64
import binascii
import logging
from datetime import timedelta
from typing import TYPE_CHECKING, Final

from pydantic import HttpUrl

from enikk_contracts.agents import Execution
from enikk_contracts.common import ConnectorKind, ConversationRef
from enikk_contracts.delegation import (
    ASK_EVENT_PREFIX,
    Delegated,
    delegated_session_id,
    delegated_thread,
)
from enikk_contracts.events import EventAccepted
from enikk_contracts.outbox import (
    SCHEDULED_KINDS,
    OutboundKind,
    OutboundMessage,
    OutboxAcked,
    OutboxBatch,
    OutboxFile,
)
from enikk_contracts.tasks import (
    HELD_EVENT_PREFIX,
    CancelResult,
    LeaseExtended,
    LeaseResponse,
    ResultAccepted,
    Succeeded,
    TaskAssignment,
    TaskStatus,
)
from enikk_contracts.thread_actions import (
    MAX_FILE_BYTES,
    MAX_FILES_PER_TASK,
    MAX_SCHEDULE_AHEAD,
    MAX_SCHEDULED,
    EmojiList,
    LinkTarget,
    MessageLink,
    ScheduledCancelled,
    ScheduledMessage,
    ScheduledMessages,
    ScheduleTarget,
    SlackWorkspaceReceived,
)
from enikk_contracts.transcripts import TranscriptAppended, TranscriptLoaded, TranscriptSubpaths
from enikk_core.slack_links import SlackMessage, event_message, permalink
from enikk_core.wakeups import delegation_event_text
from enikk_core_domain.errors import (
    InvalidRequestError,
    NotFoundError,
    PermissionDeniedError,
    UnknownAgentError,
)
from enikk_core_domain.model import OutboxFile as StoredFile
from enikk_core_domain.model import OutboxMessage, Session, Task
from enikk_core_domain.redaction import looks_secret
from enikk_core_domain.thread_context import MAX_MESSAGES, with_thread_context

if TYPE_CHECKING:
    import uuid
    from collections.abc import Awaitable, Callable
    from datetime import datetime

    from pydantic import SecretStr

    from enikk_contracts.agents import AgentDefinition
    from enikk_contracts.common import Engine
    from enikk_contracts.delegation import DelegateRequest
    from enikk_contracts.events import InboundEvent
    from enikk_contracts.memory import MemoryNote
    from enikk_contracts.outbox import OutboxAck, OutboxPoll
    from enikk_contracts.tasks import Heartbeat, LeaseRequest, ResultReport
    from enikk_contracts.thread_actions import (
        CancelScheduledRequest,
        EmojiQuery,
        MessageLinkRequest,
        ScheduledQuery,
        ScheduleMessageRequest,
        SlackWorkspaceReport,
        ThreadFileRequest,
        ThreadReactionRequest,
    )
    from enikk_contracts.transcripts import SubpathQuery, TranscriptAppend, TranscriptQuery
    from enikk_core.github_tokens import SessionGitHubTokens
    from enikk_core.mcp_tokens import McpTokenIssuer
    from enikk_core.memory import MemoryService
    from enikk_core.settings import TaskSettings
    from enikk_core.wakeups import SessionWaker
    from enikk_core_domain.model import WorkerScope
    from enikk_core_domain.ports import AgentCatalog, UnitOfWork, UnitOfWorkFactory
    from enikk_kernel.clock import Clock
    from enikk_kernel.ids import IdGenerator

    Sleep = Callable[[float], Awaitable[None]]

_log = logging.getLogger(__name__)

STARTED_TEXT: Final = "작업 중"
CANCELLED_TEXT: Final = "취소했습니다."
DELEGATED_CANCELLED_TEXT: Final = "{agent} 에게 맡긴 작업도 취소했습니다."
DELEGATED_DONE_TEXT: Final = (
    "{agent} 가 맡은 작업을 마쳤습니다. 결과는 이 스레드에서 이어서 전합니다."
)
DELEGATION_INPUT: Final = (
    "[위임] 에닉(소유자의 주 에이전트)이 소유자와 대화하다가 맡긴 일입니다. 당신의 답은 "
    "소유자가 아니라 에닉에게 갑니다. 끝나면 무엇을 했는지, 만든 파일의 절대 경로, 남은 "
    "문제를 답에 적으십시오.\n\n"
)


async def child_session(
    uow: UnitOfWork, parent: Session, target: AgentDefinition, now: datetime
) -> Session:
    """The session that does ``target``'s work for the thread session ``parent``, made
    the first time (ADR-0047 §2, ADR-0054)."""
    child_id = delegated_session_id(parent.id, target.name)
    child = await uow.sessions.get(child_id)
    if child is None:
        child = Session.start(
            session_id=child_id,
            conversation=ConversationRef(
                connector=parent.conversation.connector,
                channel=parent.conversation.channel,
                thread=delegated_thread(parent.conversation.thread, target.name),
            ),
            agent=target,
            now=now,
            reply_to=parent.conversation,
        )
        await uow.sessions.add(child)
    return child


class CoreUseCases:
    def __init__(
        self,
        *,
        uow: UnitOfWorkFactory,
        catalog: AgentCatalog,
        clock: Clock,
        ids: IdGenerator,
        policy: TaskSettings,
        sleep: Sleep,
        memory: MemoryService | None = None,
        mcp_tokens: McpTokenIssuer | None = None,
        github_tokens: SessionGitHubTokens | None = None,
        self_repository: str | None = None,
        waker: SessionWaker | None = None,
    ) -> None:
        self._uow = uow
        self._catalog = catalog
        self._clock = clock
        self._ids = ids
        self._policy = policy
        self._sleep = sleep
        self._memory = memory
        self._mcp_tokens = mcp_tokens
        self._github_tokens = github_tokens
        self._self_repository = self_repository
        self._waker = waker
        self._workspace: SlackWorkspaceReport | None = None

    # ------------------------------------------------------------ connector

    async def accept_event(self, event: InboundEvent) -> EventAccepted:
        if (
            event.event_id.startswith(HELD_EVENT_PREFIX)
            and event.conversation.connector is not ConnectorKind.SMOKE
        ):
            raise PermissionDeniedError("only the smoke check submits held events")
        async with self._uow() as uow:
            existing = await uow.tasks.find_by_source_event(event.event_id)
            if existing is not None:
                return EventAccepted(
                    task_id=existing.id,
                    session_id=existing.session_id,
                    agent=existing.agent,
                    duplicate=True,
                )
            now = self._clock.now()
            session = await uow.sessions.find_by_conversation(event.conversation)
            if session is None and event.continue_only:
                raise NotFoundError("no conversation to continue in this thread")
            definition = self._catalog.get(session.agent if session else event.agent)
            if session is None:
                if definition is None:
                    raise UnknownAgentError(f"agent '{event.agent}' is not defined")
                session = Session.start(
                    session_id=self._ids.new(),
                    conversation=event.conversation,
                    agent=definition,
                    now=now,
                )
                await uow.sessions.add(session)
            text = await self._with_thread_context(uow, session, event.text)
            task = Task.enqueue(
                task_id=self._ids.new(),
                session=session,
                source_event_id=event.event_id,
                text=text,
                max_attempts=self._policy.max_attempts,
                now=now,
                execution=definition.execution if definition else Execution.SHARED,
            )
            await uow.tasks.add(task)
            await uow.commit()
        _log.info("task %s queued for agent %s", task.id, task.agent)
        return EventAccepted(
            task_id=task.id, session_id=session.id, agent=session.agent, duplicate=False
        )

    async def _with_thread_context(self, uow: UnitOfWork, session: Session, text: str) -> str:
        """Prefix what delegates and core posted in the thread since its last task (ADR-0027)."""
        previous = await uow.tasks.latest_in_session(session.id)
        if previous is None:
            return text
        posted = await uow.outbox.posted_by_others(
            session.conversation, session.id, previous.created_at, MAX_MESSAGES
        )
        return with_thread_context(text, posted)

    async def poll_outbox(self, connector: ConnectorKind, poll: OutboxPoll) -> OutboxBatch:
        deadline = self._clock.now() + timedelta(seconds=poll.wait_seconds)
        while True:
            async with self._uow() as uow:
                pending = await uow.outbox.pending(connector, poll.limit, self._clock.now())
            if pending or self._clock.now() >= deadline:
                return OutboxBatch(messages=tuple(_outbound(m) for m in pending))
            await self._sleep(self._policy.poll_interval_seconds)

    async def ack_outbox(self, connector: ConnectorKind, ack: OutboxAck) -> OutboxAcked:
        async with self._uow() as uow:
            now = self._clock.now()
            changed = 0
            for message in await uow.outbox.get_many(connector, ack.message_ids):
                delivered = message.mark_delivered(now)
                if delivered is not message:
                    await uow.outbox.update(delivered)
                    changed += 1
                if message.kind is OutboundKind.FILE:
                    await uow.outbox.drop_file(message.id)
            await uow.commit()
        return OutboxAcked(acknowledged=changed)

    async def outbox_file(self, connector: ConnectorKind, message_id: uuid.UUID) -> OutboxFile:
        """The bytes of an undelivered ``FILE`` message of ``connector`` (ADR-0042)."""
        async with self._uow() as uow:
            found = await uow.outbox.get_many(connector, [message_id])
            file = await uow.outbox.file(message_id) if found else None
        if file is None:
            raise NotFoundError(f"no file for outbox message {message_id}")
        return OutboxFile(
            filename=file.filename, content_base64=base64.b64encode(file.content).decode()
        )

    async def cancel_task(self, connector: ConnectorKind, task_id: uuid.UUID) -> CancelResult:
        """Cancel the queued and running tasks of ``task_id``'s session (ADR-0037 3b).

        Workers find out at their next heartbeat, which core then rejects."""
        async with self._uow() as uow:
            task = await uow.tasks.get_for_update(task_id)
            session = None if task is None else await uow.sessions.get(task.session_id)
            if task is None or session is None or session.conversation.connector is not connector:
                raise NotFoundError(f"task {task_id} not found")
            now = self._clock.now()
            cancelled: list[uuid.UUID] = []
            for active in await uow.tasks.active_in_session(session.id):
                await uow.tasks.update(active.cancel(now))
                cancelled.append(active.id)
            cancelled.extend(await self._cancel_delegated(uow, session, now))
            if cancelled:
                await uow.outbox.add(
                    OutboxMessage(
                        id=self._ids.new(),
                        task_id=task.id,
                        source_event_id=task.source_event_id,
                        conversation=session.outbox_conversation,
                        kind=OutboundKind.CANCELLED,
                        text=CANCELLED_TEXT,
                        created_at=now,
                    )
                )
            await uow.commit()
        _log.info("task %s: cancelled %d task(s) of its session", task_id, len(cancelled))
        return CancelResult(task_id=task_id, cancelled=tuple(cancelled))

    async def _cancel_delegated(
        self, uow: UnitOfWork, session: Session, now: datetime
    ) -> list[uuid.UUID]:
        """Cancel the work ``session`` handed to other agents (ADR-0047). A running one
        gets a line in the thread, which also takes its [취소] button away."""
        definition = self._catalog.get(session.agent)
        cancelled: list[uuid.UUID] = []
        helpers = () if definition is None else (*definition.delegates, *definition.asks)
        for agent in helpers:
            child = await uow.sessions.get(delegated_session_id(session.id, agent))
            if child is None:
                continue
            for active in await uow.tasks.active_in_session(child.id):
                await uow.tasks.update(active.cancel(now))
                cancelled.append(active.id)
                if active.status is TaskStatus.LEASED:
                    await uow.outbox.add(
                        self._thread_message(
                            active,
                            child,
                            OutboundKind.CANCELLED,
                            DELEGATED_CANCELLED_TEXT.format(agent=agent),
                        )
                    )
        return cancelled

    # ------------------------------------------------------------ delegation

    async def delegate(
        self, scope: WorkerScope, task_id: uuid.UUID, request: DelegateRequest
    ) -> Delegated:
        """Queue ``request`` for another agent in its own session (ADR-0047). Its result
        wakes this task's thread; one hand-off per agent per task."""
        async with self._uow() as uow:
            task = await self._leased_task(uow, scope, task_id, request.lease_id)
            parent = await self._slack_session(uow, task)
            definition = self._catalog.get(parent.agent)
            if parent.reply_to is not None or definition is None:
                raise PermissionDeniedError("only a thread's own session hands work on")
            target = self._catalog.get(request.agent)
            if target is None or target.name not in definition.delegates:
                allowed = ", ".join(definition.delegates) or "none"
                raise PermissionDeniedError(
                    f"'{parent.agent}' may not hand work to '{request.agent}' (allowed: {allowed})"
                )
            event_id = f"delegate:{task.id}:{target.name}"
            existing = await uow.tasks.find_by_source_event(event_id)
            if existing is not None:
                return Delegated(
                    agent=target.name,
                    session_id=existing.session_id,
                    task_id=existing.id,
                    duplicate=True,
                )
            now = self._clock.now()
            child = await child_session(uow, parent, target, now)
            queued = Task.enqueue(
                task_id=self._ids.new(),
                session=child,
                source_event_id=event_id,
                text=DELEGATION_INPUT + request.request,
                max_attempts=self._policy.max_attempts,
                now=now,
                execution=target.execution,
            )
            await uow.tasks.add(queued)
            await uow.commit()
        _log.info("task %s handed work to %s (task %s)", task.id, target.name, queued.id)
        return Delegated(agent=target.name, session_id=child.id, task_id=queued.id, duplicate=False)

    async def _wake_delegator(self, uow: UnitOfWork, session: Session, task: Task) -> bool:
        """Wake the thread that handed ``task`` on with its result; False if it cannot."""
        if self._waker is None or session.reply_to is None:
            return False
        parent = await uow.sessions.find_by_conversation(session.reply_to)
        if parent is None:
            return False
        succeeded = task.status is TaskStatus.SUCCEEDED
        text = (task.output if succeeded else task.failure) or ""
        asked = task.source_event_id.startswith(ASK_EVENT_PREFIX)
        return await self._waker.wake(
            uow,
            parent,
            key=f"delegated:{task.id}",
            text=delegation_event_text(task.agent, succeeded=succeeded, text=text, asked=asked),
        )

    # ------------------------------------------------------------ runtime

    async def lease_task(self, engine: Engine, request: LeaseRequest) -> LeaseResponse:
        deadline = self._clock.now() + timedelta(seconds=request.wait_seconds)
        while True:
            assignment = await self._try_lease(engine, request.task_id)
            if assignment is not None or self._clock.now() >= deadline:
                return LeaseResponse(assignment=assignment)
            await self._sleep(self._policy.poll_interval_seconds)

    async def _try_lease(
        self, engine: Engine, held: uuid.UUID | None = None
    ) -> TaskAssignment | None:
        async with self._uow() as uow:
            queued = await uow.queue.claim_next(engine, held)
            if queued is None:
                return None
            session = await uow.sessions.get(queued.session_id)
            if session is None:
                raise NotFoundError(f"session {queued.session_id} of task {queued.id} is missing")
            now = self._clock.now()
            leased = queued.grant_lease(
                lease_id=self._ids.new(), now=now, duration=self._policy.lease_duration
            )
            if leased.lease is None or leased.last_lease_id is None:
                raise RuntimeError("leased task without a lease")
            definition = self._catalog.get(leased.agent)
            if (
                definition is None
                or definition.engine is not engine
                or definition.execution is not Execution.SHARED
            ):
                failed = leased.fail(
                    lease_id=leased.last_lease_id,
                    reason=f"agent '{leased.agent}' no longer runs on the shared {engine} runtime",
                    retryable=False,
                    now=now,
                )
                await uow.tasks.update(failed)
                await self._publish_outcome(uow, failed)
                await uow.commit()
                _log.warning("task %s failed: agent %s is not defined", failed.id, failed.agent)
                return None
            await uow.tasks.update(leased)
            await self._publish_started(uow, session, leased)
            await uow.commit()
        _log.info("task %s leased (attempt %d)", leased.id, leased.attempt)
        scheduled = session.conversation.connector is ConnectorKind.SCHEDULE
        memories = () if scheduled else await self._memories()
        github_token = (
            None
            if self._github_tokens is None
            else await self._github_tokens.issue(leased.id, definition)
        )
        return TaskAssignment(
            task_id=leased.id,
            lease_id=leased.last_lease_id,
            lease_expires_at=leased.lease.expires_at,
            attempt=leased.attempt,
            definition=definition,
            session_id=session.id,
            engine_session_id=session.engine_session_id,
            input=leased.input,
            memories=memories,
            mcp_token=self._mcp_token(leased.id, leased.last_lease_id, session.id, definition),
            github_token=github_token,
            one_off=scheduled,
        )

    def _mcp_token(
        self,
        task_id: uuid.UUID,
        lease_id: uuid.UUID,
        session_id: uuid.UUID,
        definition: AgentDefinition,
    ) -> SecretStr | None:
        """The task's MCP token (ADR-0039 §2). A signing failure never blocks the task."""
        if self._mcp_tokens is None:
            return None
        try:
            return self._mcp_tokens.issue(
                task_id=task_id,
                lease_id=lease_id,
                session_id=session_id,
                definition=definition,
                now=self._clock.now(),
            )
        except Exception:
            _log.exception("task %s: could not sign its MCP token", task_id)
            return None

    async def _memories(self) -> tuple[MemoryNote, ...]:
        """Enikk's memory for a task (ADR-0028). A broken index never blocks work."""
        if self._memory is None:
            return ()
        try:
            return await self._memory.for_task()
        except Exception:
            _log.exception("memory notes unavailable; the task runs without them")
            return ()

    async def heartbeat(
        self, scope: WorkerScope, task_id: uuid.UUID, beat: Heartbeat
    ) -> LeaseExtended:
        async with self._uow() as uow:
            task = await self._task_of(uow, scope, task_id)
            extended = task.extend_lease(
                lease_id=beat.lease_id,
                now=self._clock.now(),
                duration=self._policy.lease_duration,
                progress=beat.progress,
            )
            await uow.tasks.update(extended)
            await uow.commit()
        if extended.lease is None:
            raise RuntimeError("extended task without a lease")
        return LeaseExtended(lease_expires_at=extended.lease.expires_at)

    async def report_result(
        self, scope: WorkerScope, task_id: uuid.UUID, report: ResultReport
    ) -> ResultAccepted:
        async with self._uow() as uow:
            task = await self._task_of(uow, scope, task_id)
            if task.already_reported(report.lease_id):
                return ResultAccepted(task_id=task.id, status=task.status, duplicate=True)
            now = self._clock.now()
            outcome = report.outcome
            if isinstance(outcome, Succeeded):
                done = task.succeed(lease_id=report.lease_id, text=outcome.text, now=now)
                if outcome.engine_session_id is not None:
                    session = await self._session_of(uow, done)
                    await uow.sessions.update(
                        session.bind_engine_session(outcome.engine_session_id, now)
                    )
            else:
                done = task.fail(
                    lease_id=report.lease_id,
                    reason=outcome.reason,
                    retryable=outcome.retryable,
                    now=now,
                )
            await uow.tasks.update(done)
            await self._publish_outcome(uow, done)
            await uow.commit()
        _log.info("task %s reported %s", done.id, done.status)
        return ResultAccepted(task_id=done.id, status=done.status, duplicate=False)

    async def verify_lease(
        self, scope: WorkerScope, task_id: uuid.UUID, lease_id: uuid.UUID
    ) -> None:
        """Raise unless ``lease_id`` is the active lease of ``task_id`` within ``scope``."""
        async with self._uow() as uow:
            await self._leased_task(uow, scope, task_id, lease_id)

    # ------------------------------------------------------------ thread actions (ADR-0042)

    async def post_file(
        self, scope: WorkerScope, task_id: uuid.UUID, request: ThreadFileRequest
    ) -> None:
        """Queue a file for the Slack thread the task answers."""
        content = _decoded(request.content_base64)
        if _text_with_secret(content):
            raise InvalidRequestError("this file looks like it holds a secret; it was not sent")
        async with self._uow() as uow:
            task = await self._leased_task(uow, scope, task_id, request.lease_id)
            session = await self._slack_session(uow, task)
            if await uow.outbox.count_of_task(task.id, OutboundKind.FILE) >= MAX_FILES_PER_TASK:
                raise InvalidRequestError(f"at most {MAX_FILES_PER_TASK} files per task")
            message = self._thread_message(task, session, OutboundKind.FILE, request.title)
            await uow.outbox.add(message)
            await uow.outbox.add_file(
                StoredFile(message_id=message.id, filename=request.filename, content=content)
            )
            await uow.commit()
        _log.info("task %s queued file %s (%d bytes)", task.id, request.filename, len(content))

    async def react(
        self, scope: WorkerScope, task_id: uuid.UUID, request: ThreadReactionRequest
    ) -> str:
        """Queue an emoji for the owner's message the task answers; returns a note for
        the session when the name is not one of the workspace's custom emoji."""
        async with self._uow() as uow:
            task = await self._leased_task(uow, scope, task_id, request.lease_id)
            session = await self._slack_session(uow, task)
            await uow.outbox.add(
                self._thread_message(task, session, OutboundKind.REACTION, request.name)
            )
            await uow.commit()
        workspace = self._workspace
        if workspace is None or workspace.emoji is None or request.name in workspace.emoji:
            return ""
        return (
            f"{request.name} is not a custom emoji of the workspace; unless it is one of "
            "Slack's standard emoji, Slack ignores it."
        )

    # ------------------------------------------------------------ workspace, links, emoji

    def report_workspace(self, report: SlackWorkspaceReport) -> SlackWorkspaceReceived:
        """Keep the connector's latest view of the workspace, in memory only (ADR-0042)."""
        self._workspace = report
        return SlackWorkspaceReceived(emoji=None if report.emoji is None else len(report.emoji))

    async def message_link(
        self, scope: WorkerScope, task_id: uuid.UUID, request: MessageLinkRequest
    ) -> MessageLink:
        """A permalink to the thread, the request, or a pull request's approval message."""
        workspace = self._workspace
        async with self._uow() as uow:
            task = await self._leased_task(uow, scope, task_id, request.lease_id)
            session = await self._slack_session(uow, task)
            message = await self._link_target(uow, task, session, request)
        if workspace is None:
            raise NotFoundError("the workspace address is not known yet; try again later")
        return MessageLink(url=HttpUrl(permalink(str(workspace.url), message)))

    async def _link_target(
        self, uow: UnitOfWork, task: Task, session: Session, request: MessageLinkRequest
    ) -> SlackMessage:
        thread = session.outbox_conversation
        if request.target is LinkTarget.THREAD:
            return SlackMessage(channel=thread.channel, ts=thread.thread)
        if request.target is LinkTarget.REQUEST:
            found = event_message(task.source_event_id)
            if found is None:
                raise NotFoundError("this task does not answer an owner's Slack message")
            return found.model_copy(update={"thread": thread.thread})
        if request.number is None:
            raise InvalidRequestError("a pull request link needs the pull request's number")
        if self._self_repository is None:
            raise NotFoundError("the platform's repository is not configured")
        ref = await uow.review_messages.get(self._self_repository, request.number)
        if ref is None:
            raise NotFoundError(f"no approval message for pull request #{request.number}")
        return SlackMessage(channel=ref.channel, ts=ref.ts)

    async def list_emoji(
        self, scope: WorkerScope, task_id: uuid.UUID, query: EmojiQuery
    ) -> EmojiList:
        async with self._uow() as uow:
            await self._leased_task(uow, scope, task_id, query.lease_id)
        workspace = self._workspace
        if workspace is None or workspace.emoji is None:
            return EmojiList(known=False)
        return EmojiList(known=True, names=tuple(sorted(workspace.emoji)))

    # ------------------------------------------------------------ scheduled messages

    async def schedule_message(
        self, scope: WorkerScope, task_id: uuid.UUID, request: ScheduleMessageRequest
    ) -> ScheduledMessage:
        """Keep a message in the outbox until ``at`` (ADR-0042)."""
        now = self._clock.now()
        if request.at <= now:
            raise InvalidRequestError("the time must be in the future")
        if request.at > now + MAX_SCHEDULE_AHEAD:
            raise InvalidRequestError(f"at most {MAX_SCHEDULE_AHEAD.days} days ahead")
        if looks_secret(request.text):
            raise InvalidRequestError("this text looks like it holds a secret; not scheduled")
        kind = (
            OutboundKind.SCHEDULED_DM
            if request.target is ScheduleTarget.DM
            else OutboundKind.SCHEDULED
        )
        async with self._uow() as uow:
            task = await self._leased_task(uow, scope, task_id, request.lease_id)
            session = await self._slack_session(uow, task)
            if len(await uow.outbox.waiting(MAX_SCHEDULED)) >= MAX_SCHEDULED:
                raise InvalidRequestError(f"at most {MAX_SCHEDULED} scheduled messages wait")
            message = self._thread_message(task, session, kind, request.text).model_copy(
                update={"not_before": request.at}
            )
            await uow.outbox.add(message)
            await uow.commit()
        _log.info("task %s scheduled message %s for %s", task.id, message.id, request.at)
        return _scheduled(message)

    async def scheduled_messages(
        self, scope: WorkerScope, task_id: uuid.UUID, query: ScheduledQuery
    ) -> ScheduledMessages:
        """Every scheduled message still waiting, in any thread: there is one owner."""
        async with self._uow() as uow:
            await self._leased_task(uow, scope, task_id, query.lease_id)
            waiting = await uow.outbox.waiting(MAX_SCHEDULED)
        return ScheduledMessages(
            messages=tuple(_scheduled(m) for m in waiting if m.kind in SCHEDULED_KINDS)
        )

    async def cancel_scheduled(
        self, scope: WorkerScope, task_id: uuid.UUID, request: CancelScheduledRequest
    ) -> ScheduledCancelled:
        async with self._uow() as uow:
            await self._leased_task(uow, scope, task_id, request.lease_id)
            found = await uow.outbox.get_many(ConnectorKind.SLACK, [request.message_id])
            if not found or found[0].kind not in SCHEDULED_KINDS:
                raise NotFoundError(f"no scheduled message {request.message_id}")
            cancelled = await uow.outbox.delete_pending(request.message_id)
            await uow.commit()
        return ScheduledCancelled(cancelled=cancelled)

    async def _slack_session(self, uow: UnitOfWork, task: Task) -> Session:
        session = await self._session_of(uow, task)
        if session.conversation.connector is not ConnectorKind.SLACK:
            raise PermissionDeniedError("only a Slack thread takes files and reactions")
        return session

    def _thread_message(
        self, task: Task, session: Session, kind: OutboundKind, text: str
    ) -> OutboxMessage:
        return OutboxMessage(
            id=self._ids.new(),
            task_id=task.id,
            source_event_id=task.source_event_id,
            conversation=session.outbox_conversation,
            kind=kind,
            text=text,
            created_at=self._clock.now(),
        )

    # ------------------------------------------------------------ transcripts (ADR-0014)

    async def append_transcript(
        self, scope: WorkerScope, task_id: uuid.UUID, request: TranscriptAppend
    ) -> TranscriptAppended:
        async with self._uow() as uow:
            task = await self._leased_task(uow, scope, task_id, request.lease_id)
            stored = await uow.transcripts.append(task.session_id, request.key, request.entries)
            await uow.commit()
        return TranscriptAppended(stored=stored)

    async def load_transcript(
        self, scope: WorkerScope, task_id: uuid.UUID, query: TranscriptQuery
    ) -> TranscriptLoaded:
        async with self._uow() as uow:
            task = await self._leased_task(uow, scope, task_id, query.lease_id)
            entries = await uow.transcripts.load(task.session_id, query.key)
        return TranscriptLoaded(entries=tuple(entries))

    async def transcript_subpaths(
        self, scope: WorkerScope, task_id: uuid.UUID, query: SubpathQuery
    ) -> TranscriptSubpaths:
        async with self._uow() as uow:
            task = await self._leased_task(uow, scope, task_id, query.lease_id)
            subpaths = await uow.transcripts.subpaths(
                task.session_id, query.project_key, query.engine_session_id
            )
        return TranscriptSubpaths(subpaths=tuple(subpaths))

    # ------------------------------------------------------------ maintenance

    async def reap_expired_leases(self) -> int:
        async with self._uow() as uow:
            now = self._clock.now()
            expired = await uow.queue.claim_expired(now, self._policy.reaper_batch)
            for task in expired:
                reaped = task.expire(now)
                await uow.tasks.update(reaped)
                await self._publish_outcome(uow, reaped)
                _log.warning("task %s lease expired -> %s", task.id, reaped.status)
            await uow.commit()
        return len(expired)

    # ------------------------------------------------------------ helpers

    async def _task_of(self, uow: UnitOfWork, scope: WorkerScope, task_id: uuid.UUID) -> Task:
        task = await uow.tasks.get_for_update(task_id)
        if task is None or not scope.admits(task):
            raise NotFoundError(f"task {task_id} not found")
        return task

    async def _leased_task(
        self, uow: UnitOfWork, scope: WorkerScope, task_id: uuid.UUID, lease_id: uuid.UUID
    ) -> Task:
        task = await self._task_of(uow, scope, task_id)
        task.verify_lease(lease_id, self._clock.now())
        return task

    async def _session_of(self, uow: UnitOfWork, task: Task) -> Session:
        session = await uow.sessions.get(task.session_id)
        if session is None:
            raise NotFoundError(f"session {task.session_id} of task {task.id} is missing")
        return session

    async def _publish_started(self, uow: UnitOfWork, session: Session, task: Task) -> None:
        """Tell the Slack thread its first attempt started; retries say nothing."""
        if task.attempt != 1 or session.conversation.connector is not ConnectorKind.SLACK:
            return
        await uow.outbox.add(
            OutboxMessage(
                id=self._ids.new(),
                task_id=task.id,
                source_event_id=task.source_event_id,
                conversation=session.outbox_conversation,
                kind=OutboundKind.STARTED,
                text=STARTED_TEXT,
                created_at=self._clock.now(),
            )
        )

    async def _publish_outcome(self, uow: UnitOfWork, task: Task) -> None:
        if task.status is TaskStatus.SUCCEEDED and task.output is not None:
            kind, text = OutboundKind.REPLY, task.output
        elif task.status is TaskStatus.FAILED and task.failure is not None:
            kind, text = OutboundKind.FAILURE, task.failure
        else:
            return
        session = await self._session_of(uow, task)
        if session.conversation.connector is ConnectorKind.SCHEDULE:
            return  # Core handles the outcome of its own scheduled work (ADR-0035).
        if (
            session.reply_to is not None
            and await self._wake_delegator(uow, session, task)
            and kind is OutboundKind.REPLY
        ):
            text = DELEGATED_DONE_TEXT.format(agent=task.agent)
        await uow.outbox.add(
            OutboxMessage(
                id=self._ids.new(),
                task_id=task.id,
                source_event_id=task.source_event_id,
                conversation=session.outbox_conversation,
                kind=kind,
                text=text,
                created_at=self._clock.now(),
            )
        )


def _outbound(message: OutboxMessage) -> OutboundMessage:
    return OutboundMessage(
        message_id=message.id,
        task_id=message.task_id,
        source_event_id=message.source_event_id,
        conversation=message.conversation,
        kind=message.kind,
        text=message.text,
    )


def _scheduled(message: OutboxMessage) -> ScheduledMessage:
    if message.not_before is None:
        raise ValueError("not a scheduled message")
    return ScheduledMessage(
        message_id=message.id,
        at=message.not_before,
        target=(
            ScheduleTarget.DM
            if message.kind is OutboundKind.SCHEDULED_DM
            else ScheduleTarget.THREAD
        ),
        text=message.text,
    )


def _decoded(content_base64: str) -> bytes:
    try:
        content = base64.b64decode(content_base64, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise InvalidRequestError("content_base64 is not base64") from exc
    if not content or len(content) > MAX_FILE_BYTES:
        raise InvalidRequestError(f"a file must have 1 to {MAX_FILE_BYTES} bytes")
    return content


def _text_with_secret(content: bytes) -> bool:
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return looks_secret(text)
