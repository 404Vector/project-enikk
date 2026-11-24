"""End-to-end smoke test of a deployed enikk-core (ADR-0012 §7).

Uses real ServiceAccount tokens and a port-forward; creates no cluster resources.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import TYPE_CHECKING, Final

from pydantic import SecretStr

from enikk_contracts.common import ConnectorKind, ConversationRef
from enikk_contracts.events import InboundEvent
from enikk_contracts.outbox import OutboundKind, OutboxAck, OutboxPoll
from enikk_contracts.routes import AUDIENCE
from enikk_contracts.tasks import (
    HELD_EVENT_PREFIX,
    Heartbeat,
    LeaseRequest,
    ResultReport,
    Succeeded,
)
from enikk_core_client import (
    CoreClient,
    CoreForbiddenError,
    CoreUnauthenticatedError,
    CoreUnavailableError,
    StaticTokenSource,
)
from enikk_kernel.errors import EnikkError
from enikk_platform.domain import KubeTarget

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from enikk_contracts.tasks import TaskAssignment
    from enikk_ops.settings import SmokeSettings
    from enikk_platform.ports import ClusterClient

    ClientFactory = Callable[[str, SecretStr], CoreClient]

SMOKE_CHANNEL: Final = "enikk-smoke"
READY_ATTEMPTS: Final = 30


class SmokeTestError(EnikkError):
    """A smoke check did not behave as expected."""


def default_client(base_url: str, token: SecretStr) -> CoreClient:
    return CoreClient.connect(base_url, StaticTokenSource(token))


class CoreSmokeTest:
    def __init__(
        self,
        settings: SmokeSettings,
        kube_context: str,
        cluster: ClusterClient,
        client: ClientFactory = default_client,
        report: Callable[[str], None] = print,
    ) -> None:
        self._settings = settings
        self._cluster = cluster
        self._client = client
        self._report = report
        self._target = KubeTarget(context=kube_context, namespace=settings.namespace)

    def run(self) -> None:
        s = self._settings
        tokens = {
            name: self._cluster.create_token(self._target, name, audience=AUDIENCE)
            for name in (s.connector_account, s.runtime_account, s.unbound_account)
        }
        with self._cluster.forwarding(self._target, s.service, s.local_port, s.service_port):
            asyncio.run(self._checks(f"http://127.0.0.1:{s.local_port}", tokens))

    async def _checks(self, base_url: str, tokens: dict[str, SecretStr]) -> None:
        s = self._settings
        async with (
            self._client(base_url, tokens[s.connector_account]) as connector,
            self._client(base_url, tokens[s.runtime_account]) as runtime,
            self._client(base_url, tokens[s.unbound_account]) as stranger,
            self._client(base_url, _forged()) as forger,
        ):
            health = await _until_ready(connector)
            self._ok(f"core is up (version {health})")

            event = _smoke_event()
            await _expect_error(CoreUnauthenticatedError, forger.submit_event(event))
            self._ok("forged token rejected (401)")
            await _expect_error(CoreForbiddenError, stranger.submit_event(event))
            self._ok(f"ServiceAccount without a role rejected (403): {s.unbound_account}")
            await _expect_error(CoreForbiddenError, runtime.submit_event(event))
            self._ok("runtime cannot submit events (403)")

            accepted = await connector.submit_event(event)
            if accepted.duplicate or not (await connector.submit_event(event)).duplicate:
                raise SmokeTestError("event idempotency check failed")
            self._ok(f"event accepted as task {accepted.task_id}; resend deduplicated")

            assignment = await self._lease(runtime, accepted.task_id)
            self._ok(f"runtime leased the task (attempt {assignment.attempt})")
            await runtime.heartbeat(
                assignment.task_id, Heartbeat(lease_id=assignment.lease_id, progress="smoke")
            )
            report = ResultReport(lease_id=assignment.lease_id, outcome=Succeeded(text="smoke ok"))
            await runtime.report_result(assignment.task_id, report)
            if not (await runtime.report_result(assignment.task_id, report)).duplicate:
                raise SmokeTestError("result idempotency check failed")
            self._ok("heartbeat and result accepted; repeated result deduplicated")

            message_id = await self._receive(connector, accepted.task_id)
            acked = await connector.ack_outbox(OutboxAck(message_ids=(message_id,)))
            if acked.acknowledged != 1:
                raise SmokeTestError("outbox acknowledgement failed")
            self._ok("connector received the reply from the outbox and acknowledged it")

            await self.check_github(connector)

    async def check_github(self, connector: CoreClient) -> None:
        """Report whether core can act through the GitHub App (ADR-0019)."""
        health = await connector.github_health()
        if not health.configured:
            self._report(f"  --  GitHub App not configured, skipped ({health.detail})")
            return
        if not health.key_registered:
            self._report(
                "  --  GitHub App key not registered, skipped "
                "(enikk-ops secret set github.app-private-key --from-file <pem>)"
            )
            return
        if health.detail is not None:
            raise SmokeTestError(f"GitHub App check failed: {health.detail}")
        for gap in health.gaps:
            self._report(
                f"  !!  App permission {gap.permission}: needs {gap.required}, "
                f"granted {gap.granted or 'none'}"
            )
        for repository in health.repositories:
            if not repository.reachable:
                self._report(f"  !!  repository {repository.name} is not reachable by the App")
        if not health.ok:
            raise SmokeTestError("GitHub App lacks permissions or repositories (see above)")
        names = ", ".join(r.name for r in health.repositories)
        self._ok(f"GitHub App has every permission it needs and reaches {names}")

    async def _lease(self, runtime: CoreClient, task_id: uuid.UUID) -> TaskAssignment:
        """Take exactly the smoke task: a held task the real runtime never takes, and
        no other task is ever leased or closed by the smoke check."""
        response = await runtime.lease_task(LeaseRequest(wait_seconds=10, task_id=task_id))
        assignment = response.assignment
        if assignment is None or assignment.task_id != task_id:
            raise SmokeTestError(f"task {task_id} was not assigned to the runtime")
        return assignment

    async def _receive(self, connector: CoreClient, task_id: uuid.UUID) -> uuid.UUID:
        batch = await connector.poll_outbox(OutboxPoll(wait_seconds=10, limit=100))
        stale = [m.message_id for m in batch.messages if m.task_id != task_id]
        if stale and all(m.conversation.channel == SMOKE_CHANNEL for m in batch.messages):
            await connector.ack_outbox(OutboxAck(message_ids=tuple(stale)))
        for message in batch.messages:
            if message.task_id == task_id:
                return message.message_id
        raise SmokeTestError(f"no outbox message for task {task_id}")

    def _ok(self, text: str) -> None:
        self._report(f"  ok  {text}")


class ClaudeSmokeTest:
    """Talks to the deployed Claude runtime through core, as the Slack connector would.

    Sends two messages in one new thread; the second must recall a codeword from the
    first, which proves transcripts are stored in core and resumed (ADR-0014).
    """

    def __init__(
        self,
        settings: SmokeSettings,
        kube_context: str,
        cluster: ClusterClient,
        client: ClientFactory = default_client,
        report: Callable[[str], None] = print,
    ) -> None:
        self._settings = settings
        self._cluster = cluster
        self._client = client
        self._report = report
        self._target = KubeTarget(context=kube_context, namespace=settings.namespace)

    def run(self) -> None:
        s = self._settings
        token = self._cluster.create_token(self._target, s.connector_account, audience=AUDIENCE)
        with self._cluster.forwarding(self._target, s.service, s.local_port, s.service_port):
            asyncio.run(self._conversation(f"http://127.0.0.1:{s.local_port}", token))

    async def _conversation(self, base_url: str, token: SecretStr) -> None:
        async with self._client(base_url, token) as connector:
            await _until_ready(connector)
            thread = uuid.uuid4().hex
            codeword = f"enikk-{uuid.uuid4().hex[:6]}"
            first = await self._ask(
                connector,
                thread,
                f"Remember this codeword for later: {codeword}. Reply with just OK.",
            )
            self._report(f"  ok  first reply: {first[:200]}")
            second = await self._ask(
                connector,
                thread,
                "What was the codeword I gave you? Reply with only the codeword.",
            )
            if codeword not in second:
                raise SmokeTestError(
                    f"the second reply did not recall the codeword: {second[:200]}"
                )
            self._report(f"  ok  second reply recalled the codeword: {second[:200]}")

    async def _ask(
        self,
        connector: CoreClient,
        thread: str,
        text: str,
        *,
        agent: str = "enikk",
        timeout_seconds: float | None = None,
    ) -> str:
        accepted = await connector.submit_event(
            InboundEvent(
                event_id=f"smoke-{uuid.uuid4().hex}",
                conversation=ConversationRef(
                    connector=ConnectorKind.SMOKE, channel=SMOKE_CHANNEL, thread=thread
                ),
                user="enikk-smoke",
                text=text,
                agent=agent,
            )
        )
        timeout = timeout_seconds or self._settings.reply_timeout_seconds
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            batch = await connector.poll_outbox(OutboxPoll(wait_seconds=20, limit=100))
            stale = tuple(
                m.message_id
                for m in batch.messages
                if m.conversation.channel == SMOKE_CHANNEL and m.task_id != accepted.task_id
            )
            if stale:
                await connector.ack_outbox(OutboxAck(message_ids=stale))
            for message in batch.messages:
                if message.task_id == accepted.task_id:
                    await connector.ack_outbox(OutboxAck(message_ids=(message.message_id,)))
                    if message.kind is OutboundKind.FAILURE:
                        raise SmokeTestError(f"the agent failed: {message.text}")
                    return message.text
            if batch.messages:
                await asyncio.sleep(1)
        raise SmokeTestError(f"no reply for task {accepted.task_id} within {timeout:.0f}s")


def _smoke_event() -> InboundEvent:
    run = uuid.uuid4().hex
    return InboundEvent(
        event_id=f"{HELD_EVENT_PREFIX}smoke-{run}",
        conversation=ConversationRef(
            connector=ConnectorKind.SMOKE, channel=SMOKE_CHANNEL, thread=run
        ),
        user="enikk-smoke",
        text="smoke test",
        agent="enikk",
    )


def _forged() -> SecretStr:
    return SecretStr("forged-token")


async def _until_ready(client: CoreClient) -> str:
    for _ in range(READY_ATTEMPTS):
        try:
            return (await client.health()).version
        except CoreUnavailableError:
            await asyncio.sleep(1)
    raise SmokeTestError("core did not become reachable through the port-forward")


async def _expect_error(error: type[Exception], call: Awaitable[object]) -> None:
    try:
        await call
    except error:
        return
    raise SmokeTestError(f"expected {error.__name__}")
