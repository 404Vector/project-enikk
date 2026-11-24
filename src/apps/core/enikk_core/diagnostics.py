"""What enikk can see of its own state, read-only (ADR-0029).

Each query is answered from core's records, the Argo CD Applications, Pod logs of
enikk's own services or GitHub. Nothing here changes anything; text that looks like
a secret is masked before it leaves core.
"""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING, Final, Protocol

from enikk_contracts.deployments import BuildFailed, DeploymentFinished
from enikk_contracts.diagnostics import (
    AppState,
    DeploymentEvent,
    Deployments,
    LogLines,
    Pulls,
    PullStatus,
    RecentTasks,
    Service,
    TaskDetail,
    TaskSummary,
)
from enikk_core_domain.errors import DependencyUnavailableError, NotFoundError
from enikk_core_domain.redaction import redact
from enikk_github.permissions import TokenPurpose
from enikk_kernel.models import FrozenModel

if TYPE_CHECKING:
    from enikk_contracts.diagnostics import (
        DeploymentsQuery,
        LogsQuery,
        PullsQuery,
        RecentTasksQuery,
        TaskDetailQuery,
    )
    from enikk_core.approval.service import PullsApi
    from enikk_core.github import GitHubAppApi
    from enikk_core_domain.model import Task
    from enikk_core_domain.ports import ApplicationReader, UnitOfWork, UnitOfWorkFactory
    from enikk_kernel.clock import Clock

HEAD: Final = 1_500
MAX_LINE: Final = 2_000
DEPLOYMENT_KINDS: Final = ("deployment-finished", "build-failed")


class LogApi(Protocol):
    async def pods(self, namespace: str, selector: str) -> list[str]: ...
    async def tail(self, namespace: str, pod: str, since_seconds: int) -> list[str]: ...


class DiagnosticsPolicy(FrozenModel):
    namespace: str = "enikk"
    apps: tuple[str, ...] = ()
    repository: str = "project-enikk"


SELECTORS: Final = {
    Service.CORE: "app.kubernetes.io/name=enikk-core",
    Service.RUNTIME_CLAUDE: "app.kubernetes.io/name=enikk-runtime-claude",
    Service.CONNECTOR_SLACK: "app.kubernetes.io/name=enikk-connector-slack",
}


class DiagnosticsService:
    def __init__(
        self,
        *,
        uow: UnitOfWorkFactory,
        clock: Clock,
        policy: DiagnosticsPolicy,
        logs: LogApi | None = None,
        apps: ApplicationReader | None = None,
        github: GitHubAppApi | None = None,
        pulls: PullsApi | None = None,
    ) -> None:
        self._uow = uow
        self._clock = clock
        self._policy = policy
        self._logs = logs
        self._apps = apps
        self._github = github
        self._pulls = pulls

    async def recent_tasks(self, query: RecentTasksQuery) -> RecentTasks:
        since = self._clock.now() - timedelta(hours=query.hours)
        async with self._uow() as uow:
            tasks = await uow.tasks.recent(since, query.limit, query.status, query.role)
            summaries = [await _summary(uow, t) for t in tasks]
        return RecentTasks(tasks=tuple(summaries))

    async def task_detail(self, query: TaskDetailQuery) -> TaskDetail:
        async with self._uow() as uow:
            task = await uow.tasks.get_for_update(query.task_id)
            if task is None:
                raise NotFoundError(f"task {query.task_id} not found")
            summary = await _summary(uow, task)
            earlier = [
                t
                for t in await uow.tasks.recent(task.created_at - timedelta(days=7), 50)
                if t.session_id == task.session_id and t.created_at < task.created_at
            ]
            previous = await _summary(uow, earlier[0]) if earlier else None
        return TaskDetail(
            summary=summary,
            input_head=redact(task.input[:HEAD]),
            output_head=redact(task.output[:HEAD]) if task.output else None,
            previous=previous,
        )

    async def deployments(self, query: DeploymentsQuery) -> Deployments:
        async with self._uow() as uow:
            notices = await uow.notices.recent_of_kinds(DEPLOYMENT_KINDS, query.limit)
        events = []
        for notice in notices:
            body = notice.body
            if isinstance(body, DeploymentFinished):
                failed = [
                    f"{a.app}: {a.detail or a.result.value}"
                    for a in body.apps
                    if a.result.value != "succeeded"
                ]
                kind = "rollback" if body.rollback_of else "deployment"
                summary = "; ".join(failed) or "all apps succeeded"
                events.append(
                    DeploymentEvent(
                        at=notice.created_at,
                        kind=kind,
                        commit=body.commit,
                        succeeded=body.succeeded,
                        summary=redact(summary)[:MAX_LINE],
                    )
                )
            elif isinstance(body, BuildFailed):
                events.append(
                    DeploymentEvent(
                        at=notice.created_at,
                        kind="build",
                        commit=body.commit,
                        succeeded=False,
                        summary=f"{body.conclusion} {body.run_url}"[:MAX_LINE],
                    )
                )
        apps: list[AppState] = []
        if self._apps is not None:
            try:
                for obs in await self._apps.observe(self._policy.apps):
                    apps.append(
                        AppState(
                            app=obs.app,
                            image_tag=obs.image_tag,
                            sync=obs.operation_phase,
                            health=obs.health,
                            message=redact(obs.operation_message)[:MAX_LINE],
                        )
                    )
            except DependencyUnavailableError:
                apps = []
        return Deployments(events=tuple(events), apps=tuple(apps))

    async def logs(self, query: LogsQuery) -> LogLines:
        if self._logs is None:
            raise DependencyUnavailableError("log reading is not configured")
        namespace = self._policy.namespace
        selector = SELECTORS[query.service]
        pods = await self._logs.pods(namespace, selector)
        lines: list[str] = []
        for pod in pods[:3]:
            for line in await self._logs.tail(namespace, pod, query.minutes * 60):
                if query.contains is None or query.contains.lower() in line.lower():
                    lines.append(redact(f"{pod}: {line}")[:MAX_LINE])
        truncated = len(lines) > 200
        return LogLines(pods=tuple(pods), lines=tuple(lines[-200:]), truncated=truncated)

    async def pulls(self, query: PullsQuery) -> Pulls:
        if self._github is None or self._pulls is None:
            raise DependencyUnavailableError("GitHub is not configured")
        repository = self._policy.repository
        full_name = f"{self._github.owner}/{repository}"
        token = await self._github.installation_token(repository, TokenPurpose.WATCH)
        try:
            found = []
            for pull in await self._pulls.open_pulls(token):
                runs = await self._pulls.check_runs(token, pull.head_sha)
                checks = (
                    ", ".join(f"{r.name}={r.conclusion or 'running'}" for r in runs)
                    or "no checks yet"
                )
                async with self._uow() as uow:
                    approval = await uow.approvals.find(full_name, pull.number, pull.head_sha)
                state = approval.state.value if approval else "not requested yet"
                found.append(
                    PullStatus(
                        number=pull.number,
                        title=pull.title[:300],
                        url=pull.url,
                        branch=pull.branch,
                        head_sha=pull.head_sha,
                        checks=checks[:2_000],
                        approval=state,
                    )
                )
        finally:
            await self._github.revoke(token)
        return Pulls(pulls=tuple(found))


async def _summary(uow: UnitOfWork, task: Task) -> TaskSummary:
    session = await uow.sessions.get(task.session_id)
    thread = (
        f"{session.conversation.connector.value} {session.conversation.channel} "
        f"{session.conversation.thread}"
        if session
        else "unknown"
    )
    return TaskSummary(
        task_id=task.id,
        role=task.agent,
        status=task.status,
        attempt=task.attempt,
        failure=redact(task.failure)[:2_000] if task.failure else None,
        created_at=task.created_at,
        updated_at=task.updated_at,
        thread=thread,
    )
