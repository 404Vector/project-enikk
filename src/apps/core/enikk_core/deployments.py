"""Tells the owner how deployments and image builds of main went (ADR-0023).

Every tick reads the Argo CD Applications, judges the rollouts they show and reports
each settled deployment once, grouped by commit. It also reports failed image builds
of main, which never reach Argo CD. Its memory lives in the database, so a restart
(including core's own rollout) continues where the previous process stopped.

[되돌리기] on a failed deployment or build opens a revert PR (built by the GitHub
adapter without an LLM). The revert goes through the same Slack approval; when its
merge commit deploys or fails to build, the notice says it is a rollback result.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Protocol

from enikk_contracts.approvals import PullStage
from enikk_contracts.deployments import (
    AppDeployment,
    AppResult,
    BuildFailed,
    DeploymentFinished,
    PullLink,
    RevertOutcome,
    RevertResult,
)
from enikk_core_domain.deployments import DeploymentWatch, RevertRecord, WatchTiming
from enikk_core_domain.errors import DependencyUnavailableError
from enikk_core_domain.model import Notice
from enikk_github.permissions import TokenPurpose
from enikk_github.reverts import RevertRefused
from enikk_kernel.models import FrozenModel

if TYPE_CHECKING:
    from enikk_contracts.deployments import RevertRequest
    from enikk_core.github import GitHubAppApi
    from enikk_core.threads import ThreadNotes
    from enikk_core_domain.deployments import FinishedDeployment
    from enikk_core_domain.ports import ApplicationReader, UnitOfWork, UnitOfWorkFactory
    from enikk_github.activity import CommitPull, WorkflowRun
    from enikk_github.app import InstallationToken
    from enikk_github.reverts import RevertPull
    from enikk_kernel.clock import Clock
    from enikk_kernel.ids import IdGenerator

_log = logging.getLogger(__name__)


class ActivityApi(Protocol):
    async def workflow_runs(
        self, token: InstallationToken, workflow: str, branch: str = "main"
    ) -> list[WorkflowRun]: ...

    async def pull_of_commit(self, token: InstallationToken, sha: str) -> CommitPull | None: ...


class RevertApi(Protocol):
    async def open_revert(
        self, token: InstallationToken, sha: str, *, base: str = "main", note: str = ""
    ) -> RevertPull | RevertRefused: ...


class DeploymentPolicy(FrozenModel):
    apps: tuple[str, ...]
    """Argo CD Applications to watch."""
    repository: str
    """The platform's own repository, whose main the apps deploy."""
    images_workflow: str = "images.yml"
    workflows: bool = False
    """Whether development tokens of the repository may write workflow files."""
    timing: WatchTiming


class DeploymentService:
    def __init__(
        self,
        *,
        uow: UnitOfWorkFactory,
        reader: ApplicationReader,
        app: GitHubAppApi,
        activity: ActivityApi,
        reverter: RevertApi,
        policy: DeploymentPolicy,
        clock: Clock,
        ids: IdGenerator,
        threads: ThreadNotes | None = None,
    ) -> None:
        self._uow = uow
        self._threads = threads
        self._reader = reader
        self._app = app
        self._activity = activity
        self._reverter = reverter
        self._policy = policy
        self._clock = clock
        self._ids = ids

    async def watch_once(self) -> None:
        observations = await self._reader.observe(self._policy.apps)
        token = await self._token()
        bodies: list[DeploymentFinished | BuildFailed] = []
        try:
            runs = await self._image_runs(token)
            running = {o.image_tag for o in observations if o.image_tag}
            awaiting = frozenset(
                r.head_sha
                for r in runs
                if (not r.completed or r.conclusion == "success")
                and f"sha-{r.head_sha}" not in running
            )
            async with self._uow() as uow:
                watch = await uow.deployment_watch.load_for_update() or DeploymentWatch()
                now = self._clock.now()
                watch, finished = watch.observe(observations, now, self._policy.timing, awaiting)
                for deployment in finished:
                    notice, branch = await self._deployment_notice(token, deployment, watch)
                    bodies.append(notice)
                    stage = PullStage.DEPLOYED if notice.succeeded else PullStage.DEPLOY_FAILED
                    await self._card(uow, notice.repository, notice.pull, branch, stage, notice)
                    await self._rollback_card(uow, token, notice)
                if token is not None:
                    watch, failed = await self._failed_builds(token, watch, runs)
                    for build, branch in failed:
                        bodies.append(build)
                        await self._card(
                            uow, build.repository, build.pull, branch, PullStage.BUILD_FAILED, build
                        )
                        await self._rollback_card(uow, token, build)
                for body in bodies:
                    await uow.notices.add(Notice(id=self._ids.new(), body=body, created_at=now))
                await uow.deployment_watch.save(watch)
                await uow.commit()
        finally:
            if token is not None:
                await self._app.revoke(token)
        for body in bodies:
            _log.info("%s %s", body.kind, body.commit[:7])

    async def _card(
        self,
        uow: UnitOfWork,
        repository: str,
        pull: PullLink | None,
        branch: str | None,
        stage: PullStage,
        notice: DeploymentFinished | BuildFailed,
    ) -> None:
        """Update the status card of the PR behind ``branch`` (ADR-0036)."""
        if self._threads is None or pull is None or branch is None:
            return
        await self._threads.card(
            uow,
            branch,
            repository=repository,
            number=pull.number,
            url=pull.url,
            title=pull.title,
            stage=stage,
            commit=notice.commit,
            detail=_card_detail(notice),
        )

    async def _rollback_card(
        self,
        uow: UnitOfWork,
        token: InstallationToken | None,
        notice: DeploymentFinished | BuildFailed,
    ) -> None:
        """A rollback result also updates the card of the PR that was rolled back."""
        if notice.rollback_of is None:
            return
        found = await self._commit_pull(token, notice.rollback_of)
        if found is None:
            return
        pull, branch = found
        rolled_back = isinstance(notice, DeploymentFinished) and notice.succeeded
        stage = PullStage.ROLLED_BACK if rolled_back else PullStage.ROLLBACK_FAILED
        await self._card(uow, notice.repository, pull, branch, stage, notice)

    async def _token(self) -> InstallationToken | None:
        try:
            return await self._app.installation_token(self._policy.repository, TokenPurpose.WATCH)
        except DependencyUnavailableError as exc:
            _log.warning("deployment notices without GitHub details: %s", exc)
            return None

    async def _commit_pull(
        self, token: InstallationToken | None, sha: str
    ) -> tuple[PullLink, str] | None:
        if token is None:
            return None
        try:
            pull = await self._activity.pull_of_commit(token, sha)
        except DependencyUnavailableError as exc:
            _log.warning("cannot find the pull request of %s: %s", sha[:7], exc)
            return None
        if pull is None:
            return None
        link = PullLink(number=pull.number, title=pull.title[:300], url=pull.url[:500])
        return link, pull.branch

    async def _deployment_notice(
        self,
        token: InstallationToken | None,
        deployment: FinishedDeployment,
        watch: DeploymentWatch,
    ) -> tuple[DeploymentFinished, str | None]:
        found = await self._commit_pull(token, deployment.commit)
        pull, branch = found if found else (None, None)
        notice = DeploymentFinished(
            repository=self._full_name,
            commit=deployment.commit,
            pull=pull,
            succeeded=deployment.succeeded,
            rollback_of=watch.rollback_target(pull.number) if pull else None,
            apps=tuple(
                AppDeployment(
                    app=r.app[:100],
                    result=AppResult(r.outcome.value if r.outcome else AppResult.FAILED),
                    detail=r.detail,
                )
                for r in deployment.rollouts
            ),
        )
        return notice, branch

    async def _image_runs(self, token: InstallationToken | None) -> list[WorkflowRun]:
        """The recent image builds on main; none without GitHub."""
        if token is None:
            return []
        try:
            return await self._activity.workflow_runs(token, self._policy.images_workflow)
        except DependencyUnavailableError as exc:
            _log.warning("cannot read image builds: %s", exc)
            return []

    async def _failed_builds(
        self, token: InstallationToken, watch: DeploymentWatch, runs: list[WorkflowRun]
    ) -> tuple[DeploymentWatch, list[tuple[BuildFailed, str | None]]]:
        fresh = [r for r in runs if r.completed and watch.is_new_build_run(r.id, r.created_at)]
        notices = []
        for run in fresh:
            if not run.build_failed:
                continue
            found = await self._commit_pull(token, run.head_sha)
            pull, branch = found if found else (None, None)
            notices.append(
                (
                    BuildFailed(
                        repository=self._full_name,
                        commit=run.head_sha,
                        pull=pull,
                        run_url=run.url[:500],
                        conclusion=(run.conclusion or "unknown")[:50],
                        rollback_of=watch.rollback_target(pull.number) if pull else None,
                    ),
                    branch,
                )
            )
        return watch.saw_build_runs([r.id for r in fresh]), notices

    # ------------------------------------------------------------ reverting

    async def request_revert(self, request: RevertRequest) -> RevertResult:
        """Open (or find) the revert PR of ``request.commit``. The owner pressed the button."""
        token = await self._app.installation_token(
            self._policy.repository, TokenPurpose.DEVELOP, workflows=self._policy.workflows
        )
        try:
            async with self._uow() as uow:
                watch = await uow.deployment_watch.load_for_update() or DeploymentWatch()
                note = "배포 또는 빌드가 실패해 소유자가 Slack 에서 되돌리기를 요청했습니다."
                try:
                    outcome = await self._reverter.open_revert(token, request.commit, note=note)
                except DependencyUnavailableError as exc:
                    _log.warning("revert of %s failed: %s", request.commit[:7], exc)
                    return RevertResult(outcome=RevertOutcome.REFUSED, detail=str(exc)[:3000])
                if isinstance(outcome, RevertRefused):
                    detail = outcome.reason
                    if outcome.conflicts:
                        detail += ": " + ", ".join(outcome.conflicts)
                    _log.info("revert of %s refused: %s", request.commit[:7], detail)
                    return RevertResult(outcome=RevertOutcome.REFUSED, detail=detail[:3000])
                record = RevertRecord(
                    commit=request.commit,
                    pull_number=outcome.number,
                    pull_url=outcome.url,
                    requested_at=self._clock.now(),
                )
                await uow.deployment_watch.save(watch.with_revert(record))
                await uow.commit()
        finally:
            await self._app.revoke(token)
        _log.info(
            "revert of %s by the owner -> #%d%s",
            request.commit[:7],
            outcome.number,
            " (already open)" if outcome.existing else "",
        )
        return RevertResult(
            outcome=RevertOutcome.EXISTING if outcome.existing else RevertOutcome.OPENED,
            pull=PullLink(
                number=outcome.number, title=f"Revert {request.commit[:7]}", url=outcome.url
            ),
        )

    @property
    def _full_name(self) -> str:
        return f"{self._app.owner}/{self._policy.repository}"


def _card_detail(notice: DeploymentFinished | BuildFailed) -> str:
    """One line for the status card; the full result is in the deployment channel."""
    if isinstance(notice, BuildFailed):
        return (
            f"이미지 빌드 실패({notice.conclusion}). 배포는 시작되지 않았고 이전 버전이 동작합니다."
        )
    failed = ", ".join(a.app for a in notice.apps if a.result is AppResult.FAILED)
    if notice.succeeded:
        return ""
    return (
        f"실패한 앱: {failed or '알 수 없음'}. 새 Pod 가 준비되지 않은 앱은 이전 버전이 동작합니다."
    )
