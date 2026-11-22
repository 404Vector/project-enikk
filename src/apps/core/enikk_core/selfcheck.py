"""Post-deployment check of core, run by the Argo CD PostSync hook (ADR-0023 §2).

It calls the deployed core through its Service as the ``enikk-smoke`` identity.
A failure makes the hook Job fail, which fails the Argo CD sync, which the
deployment watcher reports to the owner. It changes no state: no task is
created, so the real runtime never picks up work from it.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING, Final

from pydantic import Field, SecretStr

from enikk_contracts.outbox import OutboxPoll
from enikk_core_client import (
    CoreClient,
    CoreUnauthenticatedError,
    CoreUnavailableError,
    FileTokenSource,
    StaticTokenSource,
)
from enikk_core_client.tokens import PROJECTED_TOKEN_FILE
from enikk_kernel.errors import EnikkError
from enikk_kernel.settings import ServiceSettings

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from enikk_core_client import TokenSource

    ClientFactory = Callable[[str, TokenSource], CoreClient]

FORGED: Final = SecretStr("forged-token")


class SelfCheckSettings(ServiceSettings, env_prefix="ENIKK_SELFCHECK_"):
    core_url: str = "http://enikk-core.enikk.svc"
    expected_version: str
    """The image tag this sync deployed; core must already serve it."""
    token_file: Path = PROJECTED_TOKEN_FILE
    attempts: int = Field(default=30, ge=1)
    interval_seconds: float = Field(default=2.0, ge=0)


class SelfCheckError(EnikkError):
    """The deployed core did not behave as expected."""


def _connect(base_url: str, tokens: TokenSource) -> CoreClient:
    return CoreClient.connect(base_url, tokens)


class SelfCheck:
    def __init__(
        self,
        settings: SelfCheckSettings,
        client: ClientFactory = _connect,
        report: Callable[[str], None] = print,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._settings = settings
        self._client = client
        self._report = report
        self._sleep = sleep

    async def run(self) -> None:
        s = self._settings
        async with (
            self._client(s.core_url, FileTokenSource(s.token_file)) as smoke,
            self._client(s.core_url, StaticTokenSource(FORGED)) as forger,
        ):
            await self._serves_expected_version(smoke)
            try:
                await forger.poll_outbox(OutboxPoll(wait_seconds=0, limit=1))
            except CoreUnauthenticatedError:
                self._ok("forged token rejected")
            else:
                raise SelfCheckError("a forged token was accepted")
            await smoke.poll_outbox(OutboxPoll(wait_seconds=0, limit=1))
            self._ok("authenticated call reached the database")
            await self._github(smoke)

    async def _serves_expected_version(self, smoke: CoreClient) -> None:
        s = self._settings
        seen = "unreachable"
        for attempt in range(s.attempts):
            if attempt:
                await self._sleep(s.interval_seconds)
            try:
                health = await smoke.health()
            except CoreUnavailableError:
                continue
            seen = health.version
            if health.status == "ok" and seen == s.expected_version:
                self._ok(f"core serves {seen}")
                return
        raise SelfCheckError(f"core does not serve {s.expected_version} (last seen: {seen})")

    async def _github(self, smoke: CoreClient) -> None:
        health = await smoke.github_health()
        if not health.configured or not health.key_registered:
            self._report("  --  GitHub App not set up, skipped")
            return
        if not health.ok:
            gaps = ", ".join(g.permission for g in health.gaps)
            unreachable = ", ".join(r.name for r in health.repositories if not r.reachable)
            raise SelfCheckError(
                f"GitHub App check failed: {health.detail or ''} "
                f"missing=[{gaps}] unreachable=[{unreachable}]"
            )
        self._ok("GitHub App works")

    def _ok(self, text: str) -> None:
        self._report(f"  ok  {text}")
