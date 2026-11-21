from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING

import httpx
import pytest
from pydantic import SecretStr

from enikk_contracts.common import ConnectorKind, Engine
from enikk_contracts.credentials import CredentialName
from enikk_core.api import CoreServices, create_app
from enikk_core.credentials import CredentialIssuer
from enikk_core.security import Gatekeeper
from enikk_core.settings import TaskSettings
from enikk_core.usecases import CoreUseCases
from enikk_core_client import CoreClient, StaticTokenSource
from enikk_core_domain.auth import (
    ConnectorRole,
    RoleBinding,
    RoleDirectory,
    RuntimeRole,
    ServiceIdentity,
)
from enikk_core_domain.testing import (
    InMemoryDatabase,
    StaticAgentCatalog,
    StaticAuthenticator,
    StaticCredentialStore,
    agent,
)
from enikk_kernel.testing import ManualClock, SequentialIds

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from fastapi import FastAPI

SLACK = ServiceIdentity(namespace="enikk", name="enikk-connector-slack")
CLAUDE = ServiceIdentity(namespace="enikk", name="enikk-runtime-claude")
GEMINI = ServiceIdentity(namespace="enikk", name="enikk-runtime-gemini")
STRANGER = ServiceIdentity(namespace="enikk", name="default")

TOKENS = {"slack": SLACK, "claude": CLAUDE, "gemini": GEMINI, "stranger": STRANGER}


@dataclass
class Harness:
    app: FastAPI
    db: InMemoryDatabase
    clock: ManualClock
    usecases: CoreUseCases
    catalog: StaticAgentCatalog

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

    catalog = StaticAgentCatalog(agent("assistant", Engine.CLAUDE), agent("painter", Engine.GEMINI))
    usecases = CoreUseCases(
        uow=db.unit_of_work,
        catalog=catalog,
        clock=clock,
        ids=SequentialIds(),
        policy=TaskSettings(lease_seconds=60, max_attempts=2),
        sleep=sleep,
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
        )
    )
    store = StaticCredentialStore(
        {
            CredentialName.SLACK_BOT_TOKEN: SecretStr("xoxb-test"),
            CredentialName.SLACK_APP_TOKEN: SecretStr("xapp-test"),
        }
    )
    services = CoreServices(
        version="test",
        usecases=usecases,
        credentials=CredentialIssuer(store),
        gatekeeper=Gatekeeper(StaticAuthenticator(TOKENS), directory),
        reaper_interval_seconds=60,
    )
    return Harness(
        app=create_app(services, run_reaper=False),
        db=db,
        clock=clock,
        usecases=usecases,
        catalog=catalog,
    )


@pytest.fixture
async def slack(harness: Harness) -> AsyncIterator[CoreClient]:
    async with harness.client("slack") as client:
        yield client


@pytest.fixture
async def claude(harness: Harness) -> AsyncIterator[CoreClient]:
    async with harness.client("claude") as client:
        yield client
