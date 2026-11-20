from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from enikk_contracts.agents import AgentDefinition, PromptRef
from enikk_contracts.common import ConnectorKind, ConversationRef, Engine
from enikk_contracts.events import InboundEvent
from enikk_contracts.tasks import (
    Failed,
    LeaseRequest,
    ResultReport,
    Succeeded,
    TaskAssignment,
)


def test_agent_definition_parses_from_json_with_defaults() -> None:
    definition = AgentDefinition.model_validate_json(
        """
        {
          "name": "assistant",
          "description": "General assistant",
          "engine": "claude",
          "prompt": {"name": "assistant", "label": "production"},
          "tools": {"mcp": [{"server": "storage", "allow": ["upload_file"]}]}
        }
        """
    )
    assert definition.engine is Engine.CLAUDE
    assert definition.tools.builtin == ()
    assert definition.tools.mcp[0].allow == ("upload_file",)
    assert definition.limits.max_turns == 20


@pytest.mark.parametrize(
    "prompt",
    [
        '{"name": "p"}',
        '{"name": "p", "label": "production", "version": 3}',
    ],
)
def test_prompt_ref_requires_exactly_one_selector(prompt: str) -> None:
    with pytest.raises(ValidationError, match="exactly one"):
        PromptRef.model_validate_json(prompt)


def test_agent_definition_rejects_unknown_fields_and_self_delegation() -> None:
    base = (
        '"name": "a", "description": "d", "engine": "gemini", "prompt": {"name": "a", "version": 1}'
    )
    with pytest.raises(ValidationError):
        AgentDefinition.model_validate_json("{" + base + ', "engine_options": {}}')
    with pytest.raises(ValidationError, match="itself"):
        AgentDefinition.model_validate_json("{" + base + ', "delegates": ["a"]}')


def test_agent_name_pattern() -> None:
    with pytest.raises(ValidationError):
        AgentDefinition(
            name="Bad Name",
            description="d",
            engine=Engine.CLAUDE,
            prompt=PromptRef(name="p", label="production"),
        )


def test_inbound_event_round_trip() -> None:
    event = InboundEvent(
        event_id="Ev123",
        conversation=ConversationRef(connector=ConnectorKind.SLACK, channel="C1", thread="1.2"),
        user="U1",
        text="hello",
        agent="assistant",
    )
    assert InboundEvent.model_validate_json(event.model_dump_json()) == event


def test_result_report_discriminates_outcome() -> None:
    lease_id = uuid.uuid4()
    ok = ResultReport.model_validate_json(
        f'{{"lease_id": "{lease_id}", "outcome": {{"kind": "succeeded", "text": "done"}}}}'
    )
    assert isinstance(ok.outcome, Succeeded)
    failed = ResultReport.model_validate_json(
        f'{{"lease_id": "{lease_id}", "outcome": {{"kind": "failed", "reason": "boom"}}}}'
    )
    assert isinstance(failed.outcome, Failed)
    assert failed.outcome.retryable is False


def test_lease_request_bounds_wait() -> None:
    with pytest.raises(ValidationError):
        LeaseRequest(wait_seconds=60)


def test_assignment_requires_aware_datetime() -> None:
    with pytest.raises(ValidationError):
        TaskAssignment(
            task_id=uuid.uuid4(),
            lease_id=uuid.uuid4(),
            lease_expires_at=datetime(2026, 1, 1),
            attempt=1,
            agent="assistant",
            session_id=uuid.uuid4(),
            engine_session_id=None,
            input="hi",
        )
    TaskAssignment(
        task_id=uuid.uuid4(),
        lease_id=uuid.uuid4(),
        lease_expires_at=datetime(2026, 1, 1, tzinfo=UTC),
        attempt=1,
        agent="assistant",
        session_id=uuid.uuid4(),
        engine_session_id=None,
        input="hi",
    )
