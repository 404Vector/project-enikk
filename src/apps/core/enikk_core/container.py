"""Composition root: the only place core's adapters are constructed (P-03)."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import TYPE_CHECKING

from enikk_contracts.credentials import CredentialName
from enikk_core.api import CoreServices
from enikk_core.catalog import YamlAgentCatalog
from enikk_core.credentials import CredentialIssuer, DirectoryCredentialStore
from enikk_core.github import GitHubHealthCheck, load_repository_policy
from enikk_core.security import Gatekeeper
from enikk_core.usecases import CoreUseCases
from enikk_core_domain.auth import RoleDirectory
from enikk_github.app import GitHubApp
from enikk_k8s_auth.authenticator import TokenReviewAuthenticator, TokenReviewConfig
from enikk_kernel.clock import SystemClock
from enikk_kernel.ids import RandomIdGenerator
from enikk_persistence_sql.engine import create_engine, create_session_factory
from enikk_persistence_sql.uow import SqlUnitOfWorkFactory

if TYPE_CHECKING:
    from pydantic import SecretStr

    from enikk_core.settings import CoreSettings


def build_services(settings: CoreSettings) -> CoreServices:
    clock = SystemClock()
    engine = create_engine(settings.database.to_config())
    usecases = CoreUseCases(
        uow=SqlUnitOfWorkFactory(create_session_factory(engine)),
        catalog=YamlAgentCatalog.load(settings.agents_dir),
        clock=clock,
        ids=RandomIdGenerator(),
        policy=settings.tasks,
        sleep=asyncio.sleep,
    )
    authenticator = TokenReviewAuthenticator.in_cluster(
        TokenReviewConfig(
            api_url=settings.token_review.api_url,
            cache_ttl=timedelta(seconds=settings.token_review.cache_seconds),
        ),
        clock,
    )
    directory = RoleDirectory(tuple(b.to_binding() for b in settings.role_bindings))
    store = DirectoryCredentialStore(settings.credentials_dir)
    return CoreServices(
        version=settings.version,
        usecases=usecases,
        credentials=CredentialIssuer(store),
        gatekeeper=Gatekeeper(authenticator, directory),
        github=_github(settings, store, clock),
        reaper_interval_seconds=settings.tasks.reaper_interval_seconds,
    )


def _github(
    settings: CoreSettings, store: DirectoryCredentialStore, clock: SystemClock
) -> GitHubHealthCheck:
    policy = load_repository_policy(
        settings.github.repositories_file, self_repository=settings.github.self_repository
    )

    def private_key() -> SecretStr | None:
        return store.get(CredentialName.GITHUB_APP_PRIVATE_KEY)

    config = settings.github.to_config()
    app = None if config is None else GitHubApp.create(config, private_key, clock)
    return GitHubHealthCheck(app, policy, private_key)
