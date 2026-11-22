"""Tells the owner how deployments and image builds of main went (ADR-0023).

Every tick reads the Argo CD Applications, judges the rollouts they show and reports
each settled deployment once, grouped by commit. It also reports failed image builds
of main, which never reach Argo CD. Its memory lives in the database, so a restart
(including core's own rollout) continues where the previous process stopped.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Protocol

from enikk_contracts.deployments import (
    AppDeployment,
    AppResult,
    BuildFailed,
    DeploymentFinished,
    PullLink,
)
from enikk_core_domain.deployments import DeploymentWatch, WatchTiming
from enikk_core_domain.errors import DependencyUnavailableError
from enikk_core_domain.model import Notice
from enikk_github.permissions import TokenPurpose
from enikk_kernel.models import FrozenModel

if TYPE_CHECKING:
    from enikk_core.github import GitHubAppApi
    from enikk_core_domain.deployments import FinishedDeployment
    from enikk_core_domain.ports import ApplicationReader, UnitOfWorkFactory
    from enikk_github.activity import CommitPull, WorkflowRun
    from enikk_github.app import InstallationToken
    from enikk_kernel.clock import Clock
    from enikk_kernel.ids import IdGenerator

_log = logging.getLogger(__name__)


class ActivityApi(Protocol):
    async def workflow_runs(
        self, token: InstallationToken, workflow: str, branch: str = "main"
    ) -> list[WorkflowRun]: ...

    async def pull_of_commit(self, token: InstallationToken, sha: str) -> CommitPull | None: ...


class DeploymentPolicy(FrozenModel):
    apps: tuple[str, ...]
    """Argo CD Applications to watch."""
    repository: str
    """The platform's own repository, whose main the apps deploy."""
    images_workflow: str = "images.yml"
    timing: WatchTiming


class DeploymentService:
    def __init__(
        self,
        *,
        uow: UnitOfWorkFactory,
        reader: ApplicationReader,
        app: GitHubAppApi,
        activity: ActivityApi,
        policy: DeploymentPolicy,
        clock: Clock,
        ids: IdGenerator,
    ) -> None:
        self._uow = uow
        self._reader = reader
        self._app = app
        self._activity = activity
        self._policy = policy
        self._clock = clock
        self._ids = ids

    async def watch_once(self) -> None:
        observations = await self._reader.observe(self._policy.apps)
        token = await self._token()
        bodies: list[DeploymentFinished | BuildFailed] = []
        try:
            async with self._uow() as uow:
                watch = await uow.deployment_watch.load_for_update() or DeploymentWatch()
                now = self._clock.now()
                watch, finished = watch.observe(observations, now, self._policy.timing)
                for deployment in finished:
                    bodies.append(await self._deployment_notice(token, deployment))
                if token is not None:
                    watch, failed = await self._failed_builds(token, watch)
                    bodies.extend(failed)
                for body in bodies:
                    await uow.notices.add(Notice(id=self._ids.new(), body=body, created_at=now))
                await uow.deployment_watch.save(watch)
                await uow.commit()
        finally:
            if token is not None:
                await self._app.revoke(token)
        for body in bodies:
            _log.info("%s %s", body.kind, body.commit[:7])

    async def _token(self) -> InstallationToken | None:
        try:
            return await self._app.installation_token(self._policy.repository, TokenPurpose.WATCH)
        except DependencyUnavailableError as exc:
            _log.warning("deployment notices without GitHub details: %s", exc)
            return None

    async def _pull(self, token: InstallationToken | None, sha: str) -> PullLink | None:
        if token is None:
            return None
        try:
            pull = await self._activity.pull_of_commit(token, sha)
        except DependencyUnavailableError as exc:
            _log.warning("cannot find the pull request of %s: %s", sha[:7], exc)
            return None
        if pull is None:
            return None
        return PullLink(number=pull.number, title=pull.title[:300], url=pull.url[:500])

    async def _deployment_notice(
        self, token: InstallationToken | None, deployment: FinishedDeployment
    ) -> DeploymentFinished:
        return DeploymentFinished(
            repository=self._full_name,
            commit=deployment.commit,
            pull=await self._pull(token, deployment.commit),
            succeeded=deployment.succeeded,
            apps=tuple(
                AppDeployment(
                    app=r.app[:100],
                    result=AppResult(r.outcome.value if r.outcome else AppResult.FAILED),
                    detail=r.detail,
                )
                for r in deployment.rollouts
            ),
        )

    async def _failed_builds(
        self, token: InstallationToken, watch: DeploymentWatch
    ) -> tuple[DeploymentWatch, list[BuildFailed]]:
        try:
            runs = await self._activity.workflow_runs(token, self._policy.images_workflow)
        except DependencyUnavailableError as exc:
            _log.warning("cannot read image builds: %s", exc)
            return watch, []
        fresh = [r for r in runs if r.completed and watch.is_new_build_run(r.id, r.created_at)]
        notices = [
            BuildFailed(
                repository=self._full_name,
                commit=run.head_sha,
                pull=await self._pull(token, run.head_sha),
                run_url=run.url[:500],
                conclusion=(run.conclusion or "unknown")[:50],
            )
            for run in fresh
            if run.build_failed
        ]
        return watch.saw_build_runs([r.id for r in fresh]), notices

    @property
    def _full_name(self) -> str:
        return f"{self._app.owner}/{self._policy.repository}"
