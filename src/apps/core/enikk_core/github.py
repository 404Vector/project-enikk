"""Core's use of the GitHub App (ADR-0019): repository policy and the connection check."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Protocol

import pydantic_core
import yaml
from pydantic import ValidationError

from enikk_contracts.github import GitHubHealth, GitHubToken, PermissionGap, RepositoryCheck
from enikk_core_domain.errors import DependencyUnavailableError, PermissionDeniedError
from enikk_core_domain.repositories import RepositoryPolicy
from enikk_github.permissions import Access, TokenPurpose, required_permissions
from enikk_kernel.errors import ConfigurationError

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from pydantic import SecretStr

    from enikk_github.app import BotIdentity, InstallationToken

_log = logging.getLogger(__name__)


class GitHubAppApi(Protocol):
    async def installation_permissions(self) -> dict[str, str]: ...

    async def installation_token(
        self, repository: str, purpose: TokenPurpose, *, workflows: bool = False
    ) -> InstallationToken: ...

    async def repository_reachable(self, token: InstallationToken) -> bool: ...

    async def bot_identity(self, token: InstallationToken) -> BotIdentity: ...

    async def revoke(self, token: InstallationToken) -> None: ...

    @property
    def owner(self) -> str: ...


class DevTokenIssuer:
    """Development tokens for isolated workers (ADR-0019 §2, I-32).

    The caller must hold the lease of an isolated task; that is checked before this.
    """

    def __init__(self, app: GitHubAppApi | None, policy: RepositoryPolicy) -> None:
        self._app = app
        self._policy = policy

    async def issue(self, repository: str | None) -> GitHubToken:
        if self._app is None:
            raise DependencyUnavailableError("the GitHub App is not configured")
        target = self._policy.own if repository is None else self._policy.get(repository)
        if target is None:
            raise PermissionDeniedError(f"repository {repository} is not managed by the agents")
        token = await self._app.installation_token(
            target.name, TokenPurpose.DEVELOP, workflows=target.workflows
        )
        bot = await self._app.bot_identity(token)
        _log.info("development token for %s issued (workflows=%s)", target.name, target.workflows)
        return GitHubToken(
            token=token.token,
            owner=self._app.owner,
            repository=target.name,
            expires_at=token.expires_at,
            workflows=target.workflows,
            author_name=bot.login,
            author_email=bot.email,
        )


def load_repository_policy(path: Path, *, self_repository: str) -> RepositoryPolicy:
    """Read ``config/github.yaml``; the repository marked self must be ``self_repository``.

    ``self_repository`` comes from core's Helm values, a protected path, so a change to
    the editable list cannot move the platform's protections to another repository.
    """
    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
        policy = RepositoryPolicy.model_validate_json(pydantic_core.to_json(document))
    except (OSError, yaml.YAMLError, ValidationError) as exc:
        raise ConfigurationError(f"invalid repository list {path}: {exc}") from exc
    if policy.own.name.lower() != self_repository.lower():
        raise ConfigurationError(
            f"{path}: the repository marked self must be {self_repository}, not {policy.own.name}"
        )
    return policy


class GitHubHealthCheck:
    """Checks the installation grants what every token purpose needs, and that each
    managed repository is reachable. Issued tokens are revoked right away."""

    def __init__(
        self,
        app: GitHubAppApi | None,
        policy: RepositoryPolicy,
        private_key: Callable[[], SecretStr | None],
    ) -> None:
        self._app = app
        self._policy = policy
        self._private_key = private_key

    async def check(self) -> GitHubHealth:
        key_registered = self._private_key() is not None
        if self._app is None:
            return GitHubHealth(
                configured=False,
                key_registered=key_registered,
                detail="GitHub App ID and installation ID are not set",
            )
        if not key_registered:
            return GitHubHealth(configured=True, key_registered=False)
        app = self._app
        try:
            granted = await app.installation_permissions()
        except DependencyUnavailableError as exc:
            return GitHubHealth(configured=True, key_registered=True, detail=str(exc))
        return GitHubHealth(
            configured=True,
            key_registered=True,
            gaps=self._gaps(granted),
            repositories=tuple([await _reach(app, r.name) for r in self._policy.repositories]),
        )

    def _gaps(self, granted: dict[str, str]) -> tuple[PermissionGap, ...]:
        required = required_permissions(workflows=self._policy.any_workflows)
        return tuple(
            PermissionGap(permission=name, required=access.value, granted=granted.get(name))
            for name, access in sorted(required.items())
            if not _covers(granted.get(name), access)
        )


async def _reach(app: GitHubAppApi, repository: str) -> RepositoryCheck:
    try:
        token = await app.installation_token(repository, TokenPurpose.WATCH)
    except DependencyUnavailableError as exc:
        _log.warning("no token for %s: %s", repository, exc)
        return RepositoryCheck(name=repository, reachable=False)
    try:
        reachable = await app.repository_reachable(token)
    finally:
        await app.revoke(token)
    return RepositoryCheck(name=repository, reachable=reachable)


def _covers(granted: str | None, required: Access) -> bool:
    if granted == Access.WRITE.value:
        return True
    return granted == Access.READ.value and required is Access.READ
