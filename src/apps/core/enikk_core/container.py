"""Composition root: the only place core's adapters are constructed (P-03)."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import TYPE_CHECKING

from enikk_core.api import CoreServices
from enikk_core.catalog import YamlAgentCatalog
from enikk_core.security import Gatekeeper
from enikk_core.usecases import CoreUseCases
from enikk_core_domain.auth import RoleDirectory
from enikk_k8s_auth.authenticator import TokenReviewAuthenticator, TokenReviewConfig
from enikk_kernel.clock import SystemClock
from enikk_kernel.ids import RandomIdGenerator
from enikk_persistence_sql.engine import create_engine, create_session_factory
from enikk_persistence_sql.uow import SqlUnitOfWorkFactory

if TYPE_CHECKING:
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
    return CoreServices(
        version=settings.version,
        usecases=usecases,
        gatekeeper=Gatekeeper(authenticator, directory),
        reaper_interval_seconds=settings.tasks.reaper_interval_seconds,
    )
