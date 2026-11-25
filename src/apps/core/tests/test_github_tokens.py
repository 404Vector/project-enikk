"""GitHub tokens for sessions granted ``github`` (ADR-0041 §1)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest
from pydantic import SecretStr

from enikk_contracts.agents import PlatformTool, ToolPolicy
from enikk_contracts.common import ConnectorKind, ConversationRef
from enikk_contracts.events import InboundEvent
from enikk_contracts.tasks import LeaseRequest
from enikk_core.github_tokens import SessionGitHubTokens
from enikk_core_domain.testing import agent
from enikk_github.app import InstallationToken
from enikk_github.permissions import TokenPurpose

if TYPE_CHECKING:
    from conftest import Harness

    from enikk_contracts.agents import AgentDefinition
    from enikk_core_client.client import CoreClient

pytestmark = pytest.mark.anyio


def _granted(name: str = "assistant") -> AgentDefinition:
    return agent(name).model_copy(update={"tools": ToolPolicy(platform=(PlatformTool.GITHUB,))})


class FakeApp:
    def __init__(self, *, broken: bool = False) -> None:
        self.broken = broken
        self.issued: list[tuple[str, TokenPurpose, bool]] = []

    async def installation_token(
        self, repository: str, purpose: TokenPurpose, *, workflows: bool = False
    ) -> InstallationToken:
        if self.broken:
            raise RuntimeError("GitHub is down")
        self.issued.append((repository, purpose, workflows))
        return InstallationToken(
            token=SecretStr(f"ghs-{repository}"),
            expires_at=datetime(2026, 1, 1, 1, tzinfo=UTC),
            repository=repository,
            permissions={},
        )


async def test_a_granted_definition_gets_a_develop_token() -> None:
    app = FakeApp()
    tokens = SessionGitHubTokens(app, "project-enikk", workflows=True)
    token = await tokens.issue(uuid.uuid4(), _granted())
    assert token is not None
    assert token.get_secret_value() == "ghs-project-enikk"
    assert app.issued == [("project-enikk", TokenPurpose.DEVELOP, True)]


async def test_no_token_without_the_grant_or_when_github_fails() -> None:
    app = FakeApp()
    assert (
        await SessionGitHubTokens(app, "project-enikk", workflows=False).issue(
            uuid.uuid4(), agent("assistant")
        )
        is None
    )
    assert app.issued == []
    broken = SessionGitHubTokens(FakeApp(broken=True), "project-enikk", workflows=False)
    assert await broken.issue(uuid.uuid4(), _granted()) is None


async def test_the_lease_carries_the_token(harness: Harness, claude: CoreClient) -> None:
    harness.catalog.put(_granted())
    harness.usecases._github_tokens = SessionGitHubTokens(
        FakeApp(), "project-enikk", workflows=True
    )
    await harness.usecases.accept_event(
        InboundEvent(
            event_id=f"Ev-{uuid.uuid4()}",
            conversation=ConversationRef(connector=ConnectorKind.SLACK, channel="C1", thread="1.0"),
            user="U1",
            text="PR 을 열어 줘",
            agent="assistant",
        )
    )
    response = await claude.lease_task(LeaseRequest(wait_seconds=0))
    assert response.assignment is not None
    assert response.assignment.github_token is not None
    assert response.assignment.github_token.get_secret_value() == "ghs-project-enikk"
