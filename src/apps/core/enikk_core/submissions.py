"""A Slack thread's commits become a pull request, pushed by core (ADR-0037 §6b, 2b).

- The runtime holding the task's lease sends the session's branch as a git bundle.
- Core checks the task: its role is granted ``pull-requests``, runs on the shared
  runtime, and the session is a Slack thread the owner started.
- The branch comes from the session, not the request: ``agent/<session ID>``. So a
  thread can only ever push its own branch, and the PR reaches the approval flow and
  the thread's status card like a developer's PR (ADR-0025, ADR-0036).
- Core applies the bundle in its own clone (``enikk_github.clone``), never in the
  shared volume, with a token only it holds. A second submission from the same thread
  updates the same branch and PR.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import logging
from typing import TYPE_CHECKING, Final, Protocol

from enikk_contracts.agents import Execution, PlatformTool
from enikk_contracts.common import ConnectorKind
from enikk_contracts.pulls import MAX_BUNDLE_BYTES, PullSubmitted
from enikk_core_domain.errors import (
    InvalidRequestError,
    NotFoundError,
    PermissionDeniedError,
)
from enikk_github.clone import Author
from enikk_github.permissions import TokenPurpose
from enikk_kernel.models import FrozenModel

if TYPE_CHECKING:
    import uuid

    from pydantic import SecretStr

    from enikk_contracts.pulls import PullSubmission
    from enikk_core.github import GitHubAppApi
    from enikk_core_domain.ports import AgentCatalog, UnitOfWorkFactory
    from enikk_github.app import InstallationToken
    from enikk_github.clone import Pushed
    from enikk_github.pulls import PullInfo

AGENT_PREFIX: Final = "agent/"
SESSION_REF_PREFIX: Final = "refs/heads/session/"
DEFAULT_BODY: Final = (
    "에닉이 Slack 스레드의 작업 공간에서 만든 변경입니다(ADR-0037 §6b). "
    "core 가 커밋을 검사하고 이 브랜치에 push 했습니다."
)

_log = logging.getLogger(__name__)


class Clone(Protocol):
    """``enikk_github.clone.CoreClone``. Blocking."""

    def push(
        self,
        bundle: bytes,
        *,
        session_ref: str,
        base: str,
        head: str,
        branch: str,
        token: SecretStr,
        author: Author,
        co_author: str,
    ) -> Pushed: ...

    def subject(self, commit: str) -> str: ...


class PullApi(Protocol):
    """``enikk_github.pulls.PullRequests``."""

    async def open_for_branch(self, token: InstallationToken, branch: str) -> PullInfo | None: ...

    async def create(
        self, token: InstallationToken, *, branch: str, title: str, body: str, base: str = "main"
    ) -> PullInfo: ...

    async def edit(
        self, token: InstallationToken, number: int, *, title: str | None, body: str | None
    ) -> PullInfo: ...


class SubmissionPolicy(FrozenModel):
    repository: str
    """The platform's own repository (``github.selfRepository``)."""
    base_branch: str = "main"
    co_author: str = "Claude <noreply@anthropic.com>"


class PullSubmissions:
    def __init__(
        self,
        *,
        uow: UnitOfWorkFactory,
        catalog: AgentCatalog,
        app: GitHubAppApi,
        pulls: PullApi,
        clone: Clone,
        policy: SubmissionPolicy,
    ) -> None:
        self._uow = uow
        self._catalog = catalog
        self._app = app
        self._pulls = pulls
        self._clone = clone
        self._policy = policy

    async def submit(self, task_id: uuid.UUID, request: PullSubmission) -> PullSubmitted:
        session_id = await self._session_of(task_id)
        bundle = _decode(request.bundle)
        branch = f"{AGENT_PREFIX}{session_id}"
        token = await self._app.installation_token(self._policy.repository, TokenPurpose.SUBMIT)
        try:
            bot = await self._app.bot_identity(token)
            pushed = await asyncio.to_thread(
                self._clone.push,
                bundle,
                session_ref=f"{SESSION_REF_PREFIX}{session_id}",
                base=request.base,
                head=request.head,
                branch=branch,
                token=token.token,
                author=Author(name=bot.login, email=bot.email),
                co_author=self._policy.co_author,
            )
            existing = await self._pulls.open_for_branch(token, branch)
            if existing is not None:
                pull = existing
                if request.title is not None or request.body is not None:
                    pull = await self._pulls.edit(
                        token, existing.number, title=request.title, body=request.body
                    )
            else:
                subject = request.title
                if subject is None:
                    subject = await asyncio.to_thread(self._clone.subject, pushed.head)
                pull = await self._pulls.create(
                    token,
                    branch=branch,
                    title=subject[:200],
                    body=request.body or DEFAULT_BODY,
                    base=self._policy.base_branch,
                )
        finally:
            await self._app.revoke(token)
        _log.info(
            "task %s: pushed %d commit(s) to %s as %s, PR #%d (%s)",
            task_id,
            pushed.commits,
            branch,
            pushed.head,
            pull.number,
            "updated" if existing is not None else "created",
        )
        return PullSubmitted(
            number=pull.number,
            url=pull.url,
            branch=branch,
            head=pushed.head,
            commits=pushed.commits,
            created=existing is None,
        )

    async def _session_of(self, task_id: uuid.UUID) -> uuid.UUID:
        async with self._uow() as uow:
            task = await uow.tasks.get_for_update(task_id)
            session = None if task is None else await uow.sessions.get(task.session_id)
        if task is None or session is None:
            raise NotFoundError(f"task {task_id} not found")
        definition = self._catalog.get(task.agent)
        if definition is None or PlatformTool.PULL_REQUESTS not in definition.tools.platform:
            raise PermissionDeniedError(f"role '{task.agent}' may not submit pull requests")
        if definition.execution is not Execution.SHARED:
            raise PermissionDeniedError("only shared-runtime sessions submit through core")
        if session.conversation.connector is not ConnectorKind.SLACK or session.reply_to:
            raise PermissionDeniedError("only a Slack thread the owner started may submit")
        return session.id


def _decode(bundle: str) -> bytes:
    try:
        data = base64.b64decode(bundle, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise InvalidRequestError("the bundle is not base64") from exc
    if len(data) > MAX_BUNDLE_BYTES:
        raise InvalidRequestError(f"the bundle is larger than {MAX_BUNDLE_BYTES} bytes")
    if not data:
        raise InvalidRequestError("the bundle is empty")
    return data
