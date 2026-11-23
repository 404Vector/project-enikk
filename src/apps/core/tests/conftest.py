from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import httpx
import pytest
from pydantic import SecretStr

from enikk_contracts.agents import Execution
from enikk_contracts.common import ConnectorKind, Engine
from enikk_contracts.credentials import CredentialName
from enikk_core.api import CoreServices, create_app
from enikk_core.credentials import CredentialIssuer
from enikk_core.dispatcher import DevJobDispatcher
from enikk_core.github import DevTokenIssuer, GitHubHealthCheck
from enikk_core.security import Gatekeeper
from enikk_core.settings import DevAgentSettings, TaskSettings
from enikk_core.threads import ReviewLinks, ThreadNotes
from enikk_core.usecases import CoreUseCases
from enikk_core_client import CoreClient, StaticTokenSource
from enikk_core_domain.auth import (
    ConnectorRole,
    DevAgentRole,
    RoleBinding,
    RoleDirectory,
    RuntimeRole,
    ServiceIdentity,
)
from enikk_core_domain.repositories import RepositoryPolicy
from enikk_core_domain.testing import (
    InMemoryDatabase,
    StaticAgentCatalog,
    StaticAuthenticator,
    StaticCredentialStore,
    agent,
)
from enikk_github.app import BotIdentity, InstallationToken
from enikk_github.permissions import TokenPurpose, required_permissions
from enikk_kernel.ids import RandomIdGenerator
from enikk_kernel.testing import ManualClock, SequentialIds

if TYPE_CHECKING:
    import uuid
    from collections.abc import AsyncIterator

    from fastapi import FastAPI

SLACK = ServiceIdentity(namespace="enikk", name="enikk-connector-slack")
CLAUDE = ServiceIdentity(namespace="enikk", name="enikk-runtime-claude")
GEMINI = ServiceIdentity(namespace="enikk", name="enikk-runtime-gemini")
STRANGER = ServiceIdentity(namespace="enikk", name="default")
SMOKE = ServiceIdentity(namespace="enikk", name="enikk-smoke")
DEV = ServiceIdentity(namespace="enikk-dev", name="enikk-dev-agent")

TOKENS = {
    "slack": SLACK,
    "claude": CLAUDE,
    "gemini": GEMINI,
    "stranger": STRANGER,
    "smoke": SMOKE,
    "dev": DEV,
}

POLICY = RepositoryPolicy.model_validate_json(
    '{"repositories": [{"name": "project-enikk", "self": true},'
    ' {"name": "side-project", "workflows": true}]}'
)


class FakeLauncher:
    """Records launches; ``running`` counts launched Jobs until ``finish_all``."""

    def __init__(self) -> None:
        self.deployed = True
        self.launched: list[uuid.UUID] = []
        self.finished = 0

    async def ready(self) -> bool:
        return self.deployed

    async def running(self) -> int:
        return len(self.launched) - self.finished

    async def launch(self, task_id: uuid.UUID) -> str:
        self.launched.append(task_id)
        return f"enikk-dev-{task_id}"

    def finish_all(self) -> None:
        self.finished = len(self.launched)


class FakeGitHubApp:
    """Grants everything required; repositories in ``unreachable`` cannot be read."""

    def __init__(self) -> None:
        self.permissions: dict[str, str] = {
            name: access.value for name, access in required_permissions(workflows=True).items()
        }
        self.unreachable: set[str] = set()
        self.issued: list[tuple[str, TokenPurpose]] = []
        self.workflows: list[bool] = []
        self.revoked: list[str] = []

    async def installation_permissions(self) -> dict[str, str]:
        return dict(self.permissions)

    async def installation_token(
        self, repository: str, purpose: TokenPurpose, *, workflows: bool = False
    ) -> InstallationToken:
        self.issued.append((repository, purpose))
        self.workflows.append(workflows)
        return InstallationToken(
            token=SecretStr(f"ghs-{repository}"),
            expires_at=datetime(2026, 1, 1, 1, tzinfo=UTC),
            repository=repository,
            permissions={},
        )

    async def repository_reachable(self, token: InstallationToken) -> bool:
        return token.repository not in self.unreachable

    async def revoke(self, token: InstallationToken) -> None:
        self.revoked.append(token.repository)

    async def bot_identity(self, token: InstallationToken) -> BotIdentity:
        return BotIdentity(login="enikk-deployer[bot]", user_id=42)

    @property
    def owner(self) -> str:
        return "404Vector"


@dataclass
class Harness:
    app: FastAPI
    db: InMemoryDatabase
    clock: ManualClock
    usecases: CoreUseCases
    catalog: StaticAgentCatalog
    github: FakeGitHubApp
    launcher: FakeLauncher
    dispatcher: DevJobDispatcher
    services: CoreServices

    def client(self, token: str) -> CoreClient:
        return CoreClient.connect(
            "http://core.test",
            StaticTokenSource(SecretStr(token)),
            transport=httpx.ASGITransport(app=self.app),
        )


@pytest.fixture
def harness() -> Harness:
    db = InMemoryDatabase()
    clock = ManualClock()

    async def sleep(seconds: float) -> None:
        clock.advance(timedelta(seconds=seconds))

    catalog = StaticAgentCatalog(
        agent("assistant", Engine.CLAUDE),
        agent("painter", Engine.GEMINI),
        agent("developer", Engine.CLAUDE, Execution.ISOLATED),
    )
    usecases = CoreUseCases(
        uow=db.unit_of_work,
        catalog=catalog,
        clock=clock,
        ids=SequentialIds(),
        policy=TaskSettings(lease_seconds=60, max_attempts=2),
        sleep=sleep,
        isolated_daily_limit=3,
    )
    directory = RoleDirectory(
        (
            RoleBinding(
                identity=SLACK,
                role=ConnectorRole(connector=ConnectorKind.SLACK),
                credentials=frozenset(
                    {CredentialName.SLACK_BOT_TOKEN, CredentialName.SLACK_APP_TOKEN}
                ),
            ),
            RoleBinding(
                identity=CLAUDE,
                role=RuntimeRole(engine=Engine.CLAUDE),
                credentials=frozenset({CredentialName.CLAUDE_OAUTH_TOKEN}),
            ),
            RoleBinding(identity=GEMINI, role=RuntimeRole(engine=Engine.GEMINI)),
            RoleBinding(identity=SMOKE, role=ConnectorRole(connector=ConnectorKind.SMOKE)),
            RoleBinding(identity=DEV, role=DevAgentRole()),
        )
    )
    store = StaticCredentialStore(
        {
            CredentialName.SLACK_BOT_TOKEN: SecretStr("xoxb-test"),
            CredentialName.SLACK_APP_TOKEN: SecretStr("xapp-test"),
        }
    )
    github = FakeGitHubApp()
    launcher = FakeLauncher()
    dispatcher = DevJobDispatcher(
        uow=db.unit_of_work,
        launcher=launcher,
        clock=clock,
        ids=RandomIdGenerator(),
        policy=DevAgentSettings(enabled=True, daily_limit=3),
    )
    services = CoreServices(
        version="test",
        usecases=usecases,
        credentials=CredentialIssuer(store),
        gatekeeper=Gatekeeper(StaticAuthenticator(TOKENS), directory),
        github=GitHubHealthCheck(github, POLICY, lambda: SecretStr("pem")),
        dev_tokens=DevTokenIssuer(github, POLICY),
        reaper_interval_seconds=60,
        dispatcher=dispatcher,
        review_links=ReviewLinks(db.unit_of_work, ThreadNotes(clock, RandomIdGenerator())),
    )
    return Harness(
        app=create_app(services, run_reaper=False),
        db=db,
        clock=clock,
        usecases=usecases,
        catalog=catalog,
        github=github,
        launcher=launcher,
        dispatcher=dispatcher,
        services=services,
    )


@pytest.fixture
async def slack(harness: Harness) -> AsyncIterator[CoreClient]:
    async with harness.client("slack") as client:
        yield client


@pytest.fixture
async def claude(harness: Harness) -> AsyncIterator[CoreClient]:
    async with harness.client("claude") as client:
        yield client
