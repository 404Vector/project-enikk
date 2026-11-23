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
from enikk_core.diagnostics import DiagnosticsPolicy, DiagnosticsService
from enikk_core.digests import DigestPolicy, DigestService
from enikk_core.dispatcher import DevJobDispatcher
from enikk_core.github import (
    DevTokenIssuer,
    GitHubHealthCheck,
    load_repository_policy,
    load_sensitive_paths,
)
from enikk_core.memory import MemoryPolicy, MemoryService
from enikk_core.prompts import PromptService, load_owner
from enikk_core.security import Gatekeeper
from enikk_core.submissions import PullSubmissions, SubmissionPolicy
from enikk_core.threads import ReviewLinks, ThreadNotes
from enikk_core.usecases import CoreUseCases
from enikk_core_domain.auth import RoleDirectory
from enikk_core_domain.deployments import WatchTiming
from enikk_core_domain.digests import DigestSchedule
from enikk_core_domain.memory import MemoryLayout
from enikk_github.activity import RepositoryActivity
from enikk_github.app import GitHubApp
from enikk_github.clone import CloneConfig, CoreClone
from enikk_github.pulls import PullRequests
from enikk_github.reverts import Reverter
from enikk_github.vault import VaultReader, VaultWriter
from enikk_k8s_auth.authenticator import TokenReviewAuthenticator, TokenReviewConfig
from enikk_k8s_jobs.applications import ArgoConfig, KubeApplicationReader
from enikk_k8s_jobs.launcher import KubeJobConfig, KubeJobLauncher
from enikk_k8s_jobs.logs import KubeLogReader, LogConfig
from enikk_kernel.clock import SystemClock
from enikk_kernel.ids import RandomIdGenerator
from enikk_kernel.process import SubprocessRunner
from enikk_langfuse.config import LangfuseConfig
from enikk_langfuse.registry import LangfusePromptRegistry
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
    catalog = YamlAgentCatalog.load(settings.agents_dir)
    usecases = CoreUseCases(
        uow=uow,
        catalog=catalog,
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
        diagnostics=_diagnostics(settings, store, clock, uow),
        prompts=_prompts(settings, store, catalog, clock, uow, ids),
        digests=_digests(settings, memory, catalog, clock, uow, ids),
        digest_interval_seconds=settings.digests.interval_seconds,
        submissions=_submissions(settings, store, catalog, clock, uow),
    )


def _submissions(
    settings: CoreSettings,
    store: DirectoryCredentialStore,
    catalog: YamlAgentCatalog,
    clock: SystemClock,
    uow: SqlUnitOfWorkFactory,
) -> PullSubmissions | None:
    s = settings.submissions
    config = settings.github.to_config()
    if config is None or not s.enabled:
        return None

    def private_key() -> SecretStr | None:
        return store.get(CredentialName.GITHUB_APP_PRIVATE_KEY)

    app = GitHubApp.create(config, private_key, clock)
    repository = settings.github.self_repository
    clone = CoreClone(
        CloneConfig(
            root=s.clone_dir,
            remote_url=f"https://github.com/{config.owner}/{repository}.git",
            git_timeout_seconds=s.git_timeout_seconds,
        ),
        SubprocessRunner(),
    )
    return PullSubmissions(
        uow=uow,
        catalog=catalog,
        app=app,
        pulls=PullRequests(app),
        clone=clone,
        policy=SubmissionPolicy(repository=repository, co_author=s.co_author),
    )


def _digests(
    settings: CoreSettings,
    memory: MemoryService | None,
    catalog: YamlAgentCatalog,
    clock: SystemClock,
    uow: SqlUnitOfWorkFactory,
    ids: RandomIdGenerator,
) -> DigestService | None:
    d = settings.digests
    if memory is None or not d.enabled:
        return None
    return DigestService(
        uow=uow,
        catalog=catalog,
        memory=memory,
        clock=clock,
        ids=ids,
        policy=DigestPolicy(
            schedule=DigestSchedule(
                timezone=d.timezone,
                run_hour=d.run_hour,
                catchup_days=d.catchup_days,
                max_auto_attempts=d.max_auto_attempts,
            ),
            agent=d.agent,
            max_task_attempts=settings.tasks.max_attempts,
        ),
    )


def _prompts(
    settings: CoreSettings,
    store: DirectoryCredentialStore,
    catalog: YamlAgentCatalog,
    clock: SystemClock,
    uow: SqlUnitOfWorkFactory,
    ids: RandomIdGenerator,
) -> PromptService | None:
    p = settings.prompts
    if not p.enabled:
        return None

    def langfuse() -> LangfuseConfig | None:
        public = store.get(CredentialName.LANGFUSE_PUBLIC_KEY)
        secret = store.get(CredentialName.LANGFUSE_SECRET_KEY)
        if public is None or secret is None:
            return None
        return LangfuseConfig(base_url=p.langfuse_url, public_key=public, secret_key=secret)

    return PromptService(
        uow=uow,
        catalog=catalog,
        registry=LangfusePromptRegistry(langfuse),
        owner=load_owner(p.slack_config_file),
        clock=clock,
        ids=ids,
    )


def _diagnostics(
    settings: CoreSettings,
    store: DirectoryCredentialStore,
    clock: SystemClock,
    uow: SqlUnitOfWorkFactory,
) -> DiagnosticsService | None:
    d = settings.diagnostics
    if not d.enabled:
        return None
    config = settings.github.to_config()

    def private_key() -> SecretStr | None:
        return store.get(CredentialName.GITHUB_APP_PRIVATE_KEY)

    app = None if config is None else GitHubApp.create(config, private_key, clock)
    return DiagnosticsService(
        uow=uow,
        clock=clock,
        policy=DiagnosticsPolicy(
            namespace=d.namespace,
            dev_namespace=d.dev_namespace,
            apps=settings.deployments.apps,
            repository=settings.github.self_repository,
        ),
        logs=KubeLogReader.in_cluster(LogConfig(api_url=d.api_url)),
        apps=KubeApplicationReader.in_cluster(
            ArgoConfig(namespace=settings.deployments.argocd_namespace, api_url=d.api_url)
        ),
        github=app,
        pulls=None if app is None else PullRequests(app),
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
