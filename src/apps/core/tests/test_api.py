"""In-process end-to-end tests: real HTTP app and typed client, in-memory persistence."""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import TYPE_CHECKING

import pytest
from fastapi import FastAPI

from enikk_contracts.common import ConnectorKind, ConversationRef
from enikk_contracts.events import InboundEvent
from enikk_contracts.health import HealthResponse
from enikk_contracts.outbox import OutboundKind, OutboxAck, OutboxBatch, OutboxPoll
from enikk_contracts.tasks import (
    Failed,
    Heartbeat,
    LeaseRequest,
    ResultReport,
    Succeeded,
    TaskAssignment,
    TaskStatus,
)
from enikk_core.api import verify_declared_roles
from enikk_core_client import (
    CoreClient,
    CoreConflictError,
    CoreForbiddenError,
    CoreInvalidRequestError,
    CoreNotFoundError,
    CoreUnauthenticatedError,
)
from enikk_kernel.errors import ConfigurationError

if TYPE_CHECKING:
    from conftest import Harness

pytestmark = pytest.mark.anyio

NOW = LeaseRequest(wait_seconds=0)


def _event(event_id: str = "Ev1", thread: str = "1.0", agent: str = "assistant") -> InboundEvent:
    return InboundEvent(
        event_id=event_id,
        conversation=ConversationRef(connector=ConnectorKind.SLACK, channel="C1", thread=thread),
        user="U1",
        text=f"message {event_id}",
        agent=agent,
    )


async def _lease(client: CoreClient) -> TaskAssignment:
    response = await client.lease_task(NOW)
    assert response.assignment is not None
    return response.assignment


async def test_health_needs_no_token(harness: Harness) -> None:
    async with harness.client("") as client:
        assert await client.health() == HealthResponse(service="enikk-core", version="test")


async def test_event_to_outbox_flow(slack: CoreClient, claude: CoreClient) -> None:
    accepted = await slack.submit_event(_event())
    assert not accepted.duplicate
    assert (await slack.submit_event(_event())).task_id == accepted.task_id

    assignment = await _lease(claude)
    assert assignment.task_id == accepted.task_id
    assert assignment.attempt == 1
    assert assignment.input == "message Ev1"
    assert assignment.engine_session_id is None

    extended = await claude.heartbeat(
        assignment.task_id, Heartbeat(lease_id=assignment.lease_id, progress="working")
    )
    assert extended.lease_expires_at >= assignment.lease_expires_at

    report = ResultReport(
        lease_id=assignment.lease_id, outcome=Succeeded(text="hi!", engine_session_id="es-1")
    )
    result = await claude.report_result(assignment.task_id, report)
    assert result.status is TaskStatus.SUCCEEDED
    assert not result.duplicate
    assert (await claude.report_result(assignment.task_id, report)).duplicate

    batch = await _poll_answers(slack)
    assert [(m.task_id, m.kind, m.text) for m in batch.messages] == [
        (accepted.task_id, OutboundKind.REPLY, "hi!")
    ]
    acked = await slack.ack_outbox(OutboxAck(message_ids=(batch.messages[0].message_id,)))
    assert acked.acknowledged == 1
    assert (await _poll_answers(slack)).messages == ()

    # The next message in the thread resumes the engine session (I-11).
    await slack.submit_event(_event("Ev2"))
    assert (await _lease(claude)).engine_session_id == "es-1"


async def test_assignment_carries_the_agent_definition(
    slack: CoreClient, claude: CoreClient
) -> None:
    await slack.submit_event(_event())
    definition = (await _lease(claude)).definition
    assert definition.name == "assistant"
    assert definition.prompt.label == "production"


async def test_task_of_a_removed_agent_fails_instead_of_leasing(
    harness: Harness, slack: CoreClient, claude: CoreClient
) -> None:
    await slack.submit_event(_event())
    harness.catalog.remove("assistant")
    assert (await claude.lease_task(NOW)).assignment is None
    batch = await _poll_answers(slack)
    assert [m.kind for m in batch.messages] == [OutboundKind.FAILURE]
    assert "no longer runs" in batch.messages[0].text


async def test_long_poll_waits_then_returns_empty(harness: Harness, claude: CoreClient) -> None:
    start = harness.clock.now()
    response = await claude.lease_task(LeaseRequest(wait_seconds=5))
    assert response.assignment is None
    assert harness.clock.now() - start >= timedelta(seconds=5)


async def test_runtime_only_sees_its_engine(harness: Harness, slack: CoreClient) -> None:
    await slack.submit_event(_event(agent="painter"))
    async with harness.client("claude") as claude:
        assert (await claude.lease_task(NOW)).assignment is None
    async with harness.client("gemini") as gemini:
        assignment = await _lease(gemini)
    async with harness.client("claude") as claude:
        with pytest.raises(CoreNotFoundError):
            await claude.heartbeat(assignment.task_id, Heartbeat(lease_id=assignment.lease_id))


async def test_one_running_task_per_thread(slack: CoreClient, claude: CoreClient) -> None:
    first = await slack.submit_event(_event("Ev1", thread="t"))
    second = await slack.submit_event(_event("Ev2", thread="t"))
    other = await slack.submit_event(_event("Ev3", thread="u"))

    running = await _lease(claude)
    assert running.task_id == first.task_id
    assert (await _lease(claude)).task_id == other.task_id
    assert (await claude.lease_task(NOW)).assignment is None

    report = ResultReport(lease_id=running.lease_id, outcome=Succeeded(text="done"))
    await claude.report_result(running.task_id, report)
    assert (await _lease(claude)).task_id == second.task_id


async def test_failures_retry_then_notify(slack: CoreClient, claude: CoreClient) -> None:
    accepted = await slack.submit_event(_event())
    for attempt in (1, 2):
        assignment = await _lease(claude)
        assert assignment.attempt == attempt
        outcome = Failed(reason="engine crashed", retryable=True)
        await claude.report_result(
            assignment.task_id, ResultReport(lease_id=assignment.lease_id, outcome=outcome)
        )
    batch = await _poll_answers(slack)
    assert [(m.task_id, m.kind) for m in batch.messages] == [
        (accepted.task_id, OutboundKind.FAILURE)
    ]


async def test_expired_leases_are_reassigned(
    harness: Harness, slack: CoreClient, claude: CoreClient
) -> None:
    await slack.submit_event(_event())
    stale = await _lease(claude)
    harness.clock.advance(timedelta(seconds=61))
    assert await harness.usecases.reap_expired_leases() == 1

    fresh = await _lease(claude)
    assert fresh.task_id == stale.task_id
    assert fresh.attempt == 2
    late = ResultReport(lease_id=stale.lease_id, outcome=Succeeded(text="too late"))
    with pytest.raises(CoreConflictError):
        await claude.report_result(stale.task_id, late)

    harness.clock.advance(timedelta(seconds=61))
    assert await harness.usecases.reap_expired_leases() == 1
    batch = await _poll_answers(slack)
    assert [m.kind for m in batch.messages] == [OutboundKind.FAILURE]


async def test_outbox_names_the_answered_event(slack: CoreClient, claude: CoreClient) -> None:
    await slack.submit_event(_event("C1:1.5", thread="1.0"))
    assignment = await _lease(claude)
    report = ResultReport(lease_id=assignment.lease_id, outcome=Succeeded(text="hi"))
    await claude.report_result(assignment.task_id, report)
    batch = await _poll_answers(slack)
    assert [m.source_event_id for m in batch.messages] == ["C1:1.5"]


async def test_continue_only_needs_an_existing_conversation(slack: CoreClient) -> None:
    follow_up = _event("Ev2").model_copy(update={"continue_only": True})
    with pytest.raises(CoreNotFoundError):
        await slack.submit_event(follow_up)
    started = await slack.submit_event(_event("Ev1"))
    continued = await slack.submit_event(follow_up)
    assert continued.session_id == started.session_id
    assert not continued.duplicate


async def test_connectors_are_isolated(harness: Harness, slack: CoreClient) -> None:
    smoke_event = _event("Ev9").model_copy(
        update={
            "conversation": ConversationRef(
                connector=ConnectorKind.SMOKE, channel="enikk-smoke", thread="t"
            )
        }
    )
    with pytest.raises(CoreForbiddenError):
        await slack.submit_event(smoke_event)
    async with harness.client("smoke") as smoke:
        with pytest.raises(CoreForbiddenError):
            await smoke.submit_event(_event())
        await smoke.submit_event(smoke_event)


async def test_unknown_agent_is_rejected(slack: CoreClient) -> None:
    with pytest.raises(CoreInvalidRequestError, match="nobody"):
        await slack.submit_event(_event(agent="nobody"))


@pytest.mark.parametrize("token", ["", "forged"])
async def test_unauthenticated_calls_are_rejected(harness: Harness, token: str) -> None:
    async with harness.client(token) as client:
        with pytest.raises(CoreUnauthenticatedError):
            await client.submit_event(_event())


async def test_roles_are_enforced(harness: Harness) -> None:
    async with harness.client("stranger") as stranger:
        with pytest.raises(CoreForbiddenError):
            await stranger.submit_event(_event())
        with pytest.raises(CoreForbiddenError):
            await stranger.lease_task(NOW)
    async with harness.client("claude") as runtime:
        with pytest.raises(CoreForbiddenError):
            await runtime.submit_event(_event())
        with pytest.raises(CoreForbiddenError):
            await runtime.poll_outbox(OutboxPoll(wait_seconds=0))
    async with harness.client("slack") as connector:
        with pytest.raises(CoreForbiddenError):
            await connector.lease_task(NOW)
        with pytest.raises(CoreForbiddenError):
            await connector.report_result(
                uuid.uuid4(),
                ResultReport(lease_id=uuid.uuid4(), outcome=Succeeded(text="forged")),
            )


async def test_malformed_body_is_invalid_request(harness: Harness) -> None:
    async with harness.client("slack") as client:
        response = await client._http.post(
            "/v1/events", content='{"event_id": 1}', headers={"Content-Type": "application/json"}
        )
    assert response.status_code == 422
    assert '"invalid_request"' in response.text


def test_endpoints_without_declared_role_fail_app_creation(harness: Harness) -> None:
    verify_declared_roles(harness.app)
    rogue = FastAPI()

    @rogue.post("/v1/rogue")
    async def rogue_endpoint() -> None:
        return None

    with pytest.raises(ConfigurationError, match="/v1/rogue"):
        verify_declared_roles(rogue)


async def test_held_tasks_go_only_to_a_lease_that_names_them(
    harness: Harness, slack: CoreClient, claude: CoreClient
) -> None:
    held = InboundEvent(
        event_id="held:smoke-1",
        conversation=ConversationRef(connector=ConnectorKind.SMOKE, channel="s", thread="1"),
        user="enikk-smoke",
        text="smoke test",
        agent="assistant",
    )
    with pytest.raises(CoreForbiddenError, match="only the smoke check"):
        await slack.submit_event(held.model_copy(update={"conversation": _event().conversation}))
    async with harness.client("smoke") as smoke:
        smoke_task = await smoke.submit_event(held)
    real = await slack.submit_event(_event("Ev-real"))
    # The runtime's normal lease takes the real task, never the held one.
    assignment = await _lease(claude)
    assert assignment.task_id == real.task_id
    assert (await claude.lease_task(NOW)).assignment is None
    # A lease that names a real task gets nothing; naming the held task takes it.
    assert (await claude.lease_task(LeaseRequest(task_id=real.task_id))).assignment is None
    targeted = await claude.lease_task(LeaseRequest(wait_seconds=0, task_id=smoke_task.task_id))
    assert targeted.assignment is not None
    assert targeted.assignment.task_id == smoke_task.task_id


async def _poll_answers(client: CoreClient) -> OutboxBatch:
    """Poll the outbox, acknowledging and leaving out the "started" notes (ADR-0037 3b)."""
    batch = await client.poll_outbox(OutboxPoll(wait_seconds=0))
    started = tuple(m.message_id for m in batch.messages if m.kind is OutboundKind.STARTED)
    if started:
        await client.ack_outbox(OutboxAck(message_ids=started))
    return OutboxBatch(
        messages=tuple(m for m in batch.messages if m.kind is not OutboundKind.STARTED)
    )
