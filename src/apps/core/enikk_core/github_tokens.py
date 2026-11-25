"""GitHub tokens for enikk's sessions (ADR-0041 §1).

A definition granted ``github`` gets, with each lease, an installation token for the
platform repository: push its agent branch, open and close pull requests, read checks.
``main`` stays behind the branch rules and the owner's approval status (I-33).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Protocol

from enikk_contracts.agents import PlatformTool
from enikk_github.permissions import TokenPurpose

if TYPE_CHECKING:
    import uuid

    from pydantic import SecretStr

    from enikk_contracts.agents import AgentDefinition
    from enikk_github.app import InstallationToken

_log = logging.getLogger(__name__)


class TokenSource(Protocol):
    async def installation_token(
        self, repository: str, purpose: TokenPurpose, *, workflows: bool = False
    ) -> InstallationToken: ...


class SessionGitHubTokens:
    def __init__(self, app: TokenSource, repository: str, *, workflows: bool) -> None:
        self._app = app
        self._repository = repository
        self._workflows = workflows

    async def issue(self, task_id: uuid.UUID, definition: AgentDefinition) -> SecretStr | None:
        """The task's token, or ``None`` if not granted. A failure never blocks the task."""
        if PlatformTool.GITHUB not in definition.tools.platform:
            return None
        try:
            token = await self._app.installation_token(
                self._repository, TokenPurpose.DEVELOP, workflows=self._workflows
            )
        except Exception:
            _log.exception("task %s: no GitHub token; it runs without one", task_id)
            return None
        _log.info("task %s: GitHub token until %s", task_id, token.expires_at)
        return token.token
