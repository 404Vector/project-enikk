"""One isolated task: claim it, prepare the workspace, run the agent, report, clean up."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Final

from enikk_contracts.credentials import CredentialName, CredentialRequest
from enikk_contracts.github import GitHubTokenRequest
from enikk_contracts.tasks import Failed, ResultReport
from enikk_core_client import CoreClient, CoreConflictError, CoreNotFoundError
from enikk_core_client.credentials import wait_for_credentials
from enikk_core_client.tokens import FileTokenSource
from enikk_engine_claude.engine import ClaudeEngine, ClaudeEngineConfig, SandboxConfig
from enikk_kernel.clock import SystemClock
from enikk_kernel.errors import ConfigurationError, EnikkError
from enikk_langfuse.config import LangfuseConfig
from enikk_langfuse.prompts import LangfusePromptStore
from enikk_langfuse.tracing import LangfuseTracing
from enikk_runtime.worker import TaskWorker, WorkerPolicy

if TYPE_CHECKING:
    from pydantic import SecretStr

    from enikk_contracts.github import GitHubToken
    from enikk_contracts.tasks import TaskAssignment
    from enikk_dev_agent.settings import DevAgentSettings
    from enikk_dev_agent.workspace import GitWorkspace, Workspace

SERVICE_NAME: Final = "enikk-dev-agent"
CREDENTIALS: Final = CredentialRequest(
    names=(
        CredentialName.CLAUDE_OAUTH_TOKEN,
        CredentialName.LANGFUSE_PUBLIC_KEY,
        CredentialName.LANGFUSE_SECRET_KEY,
    )
)

_log = logging.getLogger(__name__)


def task_brief(assignment: TaskAssignment, token: GitHubToken, workspace: Workspace) -> str:
    """The request, preceded by where the agent works. Facts only; rules are in the prompt."""
    workflows = "may" if token.workflows else "must not"
    if workspace.continuing:
        branch_state = (
            "This branch already has an open pull request from earlier work in this "
            "thread (see `gh pr view`). Add commits and push to it; do not open another "
            "pull request. Mention the pull request in your reply.\n"
        )
    else:
        branch_state = "This is a new branch from main.\n"
    return (
        f"Repository: {token.owner}/{token.repository}, cloned at {workspace.repo_dir}.\n"
        f"Branch: {workspace.branch} (already checked out; push only this branch).\n"
        f"{branch_state}"
        f"You {workflows} change files under .github/workflows.\n"
        "Sensitive paths listed in config/sensitive-paths.yaml (ADRs, invariants, CI, "
        "approval code, permissions) may be changed; such PRs get a stronger approval "
        "from the owner (ADR-0024). This is the current rule and overrides anything said "
        "about protected paths earlier in this conversation.\n"
        f"Task ID: {assignment.task_id}\n"
        "\n"
        "Request:\n"
        f"{assignment.input}"
    )


def work_branch(assignment: TaskAssignment) -> str:
    """One branch per developer session, so per Slack thread (ADR-0025)."""
    return f"agent/{assignment.session_id}"


class DevAgentApp:
    def __init__(self, settings: DevAgentSettings, workspace: GitWorkspace) -> None:
        self._settings = settings
        self._workspace = workspace

    async def run(self) -> None:
        s = self._settings
        async with CoreClient.connect(s.core_url, FileTokenSource(s.token_file)) as core:
            try:
                assignment = (await core.claim_task(s.task_id)).assignment
            except CoreNotFoundError:
                _log.warning("task %s is not waiting for a worker; nothing to do", s.task_id)
                return
            if assignment is None:
                return
            _log.info("claimed task %s (attempt %d)", assignment.task_id, assignment.attempt)
            try:
                await self._run(core, assignment)
            finally:
                await asyncio.to_thread(self._workspace.revoke_and_forget)

    async def _run(self, core: CoreClient, assignment: TaskAssignment) -> None:
        s = self._settings
        try:
            values = (await wait_for_credentials(core, CREDENTIALS)).values
            token = await core.github_token(
                GitHubTokenRequest(task_id=assignment.task_id, lease_id=assignment.lease_id)
            )
            workspace = await asyncio.to_thread(
                self._workspace.prepare, token, work_branch(assignment)
            )
        except EnikkError as exc:
            await _fail(core, assignment, f"could not prepare the workspace: {exc}")
            return
        langfuse = LangfuseConfig(
            base_url=s.langfuse_url,
            public_key=_value(values, CredentialName.LANGFUSE_PUBLIC_KEY),
            secret_key=_value(values, CredentialName.LANGFUSE_SECRET_KEY),
        )
        clock = SystemClock()
        prompts = LangfusePromptStore.connect(langfuse, clock)
        tracing = LangfuseTracing.connect(langfuse, service_name=SERVICE_NAME)
        engine = ClaudeEngine(
            config=ClaudeEngineConfig(
                workdir=workspace.repo_dir,
                scratch_dir=s.scratch_dir,
                client_app="enikk-dev-agent/0.1",
                tool_env=workspace.tool_env,
                # The repository and the git/gh configuration stay reachable; the core
                # token does not (ADR-0033).
                sandbox=SandboxConfig(
                    secrets=(s.token_file, s.token_file.parent),
                    write=(s.workspace, s.tool_home),
                ),
            ),
            oauth_token=_value(values, CredentialName.CLAUDE_OAUTH_TOKEN),
            transcripts=core,
            memory=core,
            diagnostics=core,
        )
        worker = TaskWorker(
            core=core,
            prompts=prompts,
            engine=engine,
            clock=clock,
            policy=WorkerPolicy(),
            observer=tracing.observer() if s.tracing else None,
        )
        briefed = assignment.model_copy(update={"input": task_brief(assignment, token, workspace)})
        try:
            outcome = await worker.run_assignment(briefed)
            _log.info("task %s finished: %s", assignment.task_id, outcome and outcome.kind)
        finally:
            await prompts.aclose()
            tracing.shutdown()


async def _fail(core: CoreClient, assignment: TaskAssignment, reason: str) -> None:
    _log.error("task %s: %s", assignment.task_id, reason)
    report = ResultReport(
        lease_id=assignment.lease_id, outcome=Failed(reason=reason[:2000], retryable=False)
    )
    try:
        await core.report_result(assignment.task_id, report)
    except (CoreConflictError, CoreNotFoundError):
        _log.warning("task %s: lease already lost", assignment.task_id)


def _value(values: dict[CredentialName, SecretStr], name: CredentialName) -> SecretStr:
    value = values.get(name)
    if value is None:
        raise ConfigurationError(f"core did not return credential {name}")
    return value
