"""Composition root: the only place core's adapters are constructed (P-03)."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import TYPE_CHECKING

from enikk_contracts.credentials import CredentialName
from enikk_core.api import CoreServices
from enikk_core.approval.service import ApprovalService
from enikk_core.catalog import YamlAgentCatalog
from enikk_core.credentials import CredentialIssuer, DirectoryCredentialStore
from enikk_core.deployments import DeploymentPolicy, DeploymentService
from enikk_core.dispatcher import DevJobDispatcher
from enikk_core.github import (
    DevTokenIssuer,
    GitHubHealthCheck,
    load_repository_policy,
    load_sensitive_paths,
)
from enikk_core.memory import MemoryPolicy, MemoryService
from enikk_core.security import Gatekeeper
from enikk_core.threads import ReviewLinks, ThreadNotes
from enikk_core.usecases import CoreUseCases
from enikk_core_domain.auth import RoleDirectory
from enikk_core_domain.deployments import WatchTiming
from enikk_core_domain.memory import MemoryLayout
from enikk_github.activity import RepositoryActivity
from enikk_github.app import GitHubApp
from enikk_github.pulls import PullRequests
from enikk_github.reverts import Reverter
from enikk_github.vault import VaultReader, VaultWriter
from enikk_k8s_auth.authenticator import TokenReviewAuthenticator, TokenReviewConfig
from enikk_k8s_jobs.applications import ArgoConfig, KubeApplicationReader
from enikk_k8s_jobs.launcher import KubeJobConfig, KubeJobLauncher
from enikk_kernel.clock import SystemClock
from enikk_kernel.ids import RandomIdGenerator
from enikk_persistence_sql.engine import create_engine, create_session_factory
from enikk_persistence_sql.uow import SqlUnitOfWorkFactory

if TYPE_CHECKING:
    from pydantic import SecretStr

    from enikk_core.settings import CoreSettings


def build_services(settings: CoreSettings) -> CoreServices:
    clock = SystemClock()
    ids = RandomIdGenerator()
    engine = create_engine(settings.database.to_config())
    uow = SqlUnitOfWorkFactory(create_session_factory(engine))
    store = DirectoryCredentialStore(settings.credentials_dir)
    memory = _memory(settings, store, clock, uow, ids)
    usecases = CoreUseCases(
        uow=uow,
        catalog=YamlAgentCatalog.load(settings.agents_dir),
        clock=clock,
        ids=ids,
        policy=settings.tasks,
        sleep=asyncio.sleep,
        isolated_daily_limit=settings.dev_agent.daily_limit,
        memory=memory,
    )
    dispatcher = None
    dev = settings.dev_agent
    if dev.enabled:
        launcher = KubeJobLauncher.in_cluster(
            KubeJobConfig(
                namespace=dev.namespace,
                template_configmap=dev.template_configmap,
                api_url=dev.api_url,
            )
        )
        dispatcher = DevJobDispatcher(uow=uow, launcher=launcher, clock=clock, ids=ids, policy=dev)
    authenticator = TokenReviewAuthenticator.in_cluster(
        TokenReviewConfig(
            api_url=settings.token_review.api_url,
            cache_ttl=timedelta(seconds=settings.token_review.cache_seconds),
        ),
        clock,
    )
    directory = RoleDirectory(tuple(b.to_binding() for b in settings.role_bindings))
    github, dev_tokens, approvals, deployments = _github(settings, store, clock, uow, ids)
    return CoreServices(
        version=settings.version,
        usecases=usecases,
        credentials=CredentialIssuer(store),
        gatekeeper=Gatekeeper(authenticator, directory),
        github=github,
        dev_tokens=dev_tokens,
        reaper_interval_seconds=settings.tasks.reaper_interval_seconds,
        dispatcher=dispatcher,
        dispatch_interval_seconds=dev.interval_seconds,
        approvals=approvals,
        watch_interval_seconds=settings.approval.interval_seconds,
        deployments=deployments,
        deployment_interval_seconds=settings.deployments.interval_seconds,
        review_links=ReviewLinks(uow, ThreadNotes(clock, ids)),
        memory=memory,
        memory_interval_seconds=settings.memory.interval_seconds,
    )


def _memory(
    settings: CoreSettings,
    store: DirectoryCredentialStore,
    clock: SystemClock,
    uow: SqlUnitOfWorkFactory,
    ids: RandomIdGenerator,
) -> MemoryService | None:
    config = settings.github.to_config()
    if config is None or not settings.memory.enabled:
        return None

    def private_key() -> SecretStr | None:
        return store.get(CredentialName.GITHUB_APP_PRIVATE_KEY)

    app = GitHubApp.create(config, private_key, clock)
    m = settings.memory
    return MemoryService(
        uow=uow,
        app=app,
        vault=VaultReader(app),
        writer=VaultWriter(app),
        ids=ids,
        policy=MemoryPolicy(
            repository=m.repository,
            branch=m.branch,
            layout=MemoryLayout(notes_root=m.notes_root, enikk_dir=m.enikk_dir),
            context_chars=m.context_chars,
        ),
        clock=clock,
    )


def _github(
    settings: CoreSettings,
    store: DirectoryCredentialStore,
    clock: SystemClock,
    uow: SqlUnitOfWorkFactory,
    ids: RandomIdGenerator,
) -> tuple[GitHubHealthCheck, DevTokenIssuer, ApprovalService | None, DeploymentService | None]:
    policy = load_repository_policy(
        settings.github.repositories_file, self_repository=settings.github.self_repository
    )

    def private_key() -> SecretStr | None:
        return store.get(CredentialName.GITHUB_APP_PRIVATE_KEY)

    config = settings.github.to_config()
    app = None if config is None else GitHubApp.create(config, private_key, clock)
    approvals = None
    if app is not None and settings.approval.enabled:
        approvals = ApprovalService(
            uow=uow,
            app=app,
            pulls=PullRequests(app),
            policy=policy,
            sensitive=load_sensitive_paths(settings.github.sensitive_paths_file),
            threads=ThreadNotes(clock, ids),
            clock=clock,
            ids=ids,
            sleep=asyncio.sleep,
        )
    deployments = None
    watch = settings.deployments
    if app is not None and watch.enabled:
        deployments = DeploymentService(
            uow=uow,
            reader=KubeApplicationReader.in_cluster(
                ArgoConfig(namespace=watch.argocd_namespace, api_url=watch.api_url)
            ),
            app=app,
            activity=RepositoryActivity(app),
            reverter=Reverter(app),
            threads=ThreadNotes(clock, ids),
            policy=DeploymentPolicy(
                apps=watch.apps,
                repository=settings.github.self_repository,
                images_workflow=watch.images_workflow,
                workflows=policy.own.workflows,
                timing=WatchTiming(
                    timeout=timedelta(seconds=watch.timeout_seconds),
                    settle=timedelta(seconds=watch.settle_seconds),
                ),
            ),
            clock=clock,
            ids=ids,
        )
    return (
        GitHubHealthCheck(app, policy, private_key),
        DevTokenIssuer(app, policy),
        approvals,
        deployments,
    )
