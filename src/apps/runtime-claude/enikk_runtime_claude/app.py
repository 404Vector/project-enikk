"""Composition root: fetches credentials from core, assembles and runs the worker."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import shlex
import signal
from typing import TYPE_CHECKING, Final

from enikk_contracts.credentials import CredentialName, CredentialRequest
from enikk_core_client import CoreClient
from enikk_core_client.credentials import wait_for_credentials
from enikk_core_client.tokens import FileTokenSource
from enikk_engine_claude import sandbox
from enikk_engine_claude.engine import (
    SANDBOX_WRITE,
    ClaudeEngine,
    ClaudeEngineConfig,
    SandboxConfig,
)
from enikk_engine_claude.kube_access import KubeAccess
from enikk_kernel.clock import SystemClock
from enikk_kernel.errors import ConfigurationError
from enikk_kernel.process import SubprocessRunner
from enikk_langfuse.config import LangfuseConfig
from enikk_langfuse.prompts import LangfusePromptStore
from enikk_langfuse.tracing import LangfuseTracing
from enikk_runtime.pulls import SessionPulls
from enikk_runtime.worker import TaskWorker
from enikk_runtime.workspace import SessionWorkspaces

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from pathlib import Path

    from pydantic import SecretStr

    from enikk_runtime_claude.settings import RuntimeClaudeSettings

SERVICE_NAME: Final = "enikk-runtime-claude"
CREDENTIALS: Final = CredentialRequest(
    names=(
        CredentialName.CLAUDE_OAUTH_TOKEN,
        CredentialName.LANGFUSE_PUBLIC_KEY,
        CredentialName.LANGFUSE_SECRET_KEY,
    )
)

_log = logging.getLogger(__name__)


class RuntimeClaudeApp:
    def __init__(self, settings: RuntimeClaudeSettings) -> None:
        self._settings = settings

    async def run(self) -> None:
        s = self._settings
        clock = SystemClock()
        async with CoreClient.connect(s.core_url, FileTokenSource(s.token_file)) as core:
            values = (await wait_for_credentials(core, CREDENTIALS)).values
            langfuse = LangfuseConfig(
                base_url=s.langfuse_url,
                public_key=_value(values, CredentialName.LANGFUSE_PUBLIC_KEY),
                secret_key=_value(values, CredentialName.LANGFUSE_SECRET_KEY),
            )
            prompts = LangfusePromptStore.connect(langfuse, clock)
            tracing = LangfuseTracing.connect(langfuse, service_name=SERVICE_NAME)
            skills = prompts.skills() if s.skills.enabled else None
            secrets = (s.token_file, s.token_file.parent)
            config = ClaudeEngineConfig(
                workdir=s.workdir,
                scratch_dir=s.scratch_dir,
                # The core token must stay out of the model's reach (ADR-0033).
                sandbox=SandboxConfig(secrets=secrets),
                mcp_gateway=s.mcp.gateway_url if s.mcp.enabled else None,
                kube=(
                    KubeAccess(
                        token_file=s.kube.token_file, ca_file=s.kube.ca_file, server=s.kube.server
                    )
                    if s.kube.enabled
                    else None
                ),
            )
            workspaces = self._workspaces(config, clock)
            pulls = (
                SessionPulls(workspaces, core)
                if workspaces is not None and s.pulls.enabled
                else None
            )
            engine = ClaudeEngine(
                config=config,
                oauth_token=_value(values, CredentialName.CLAUDE_OAUTH_TOKEN),
                transcripts=core,
                memory=core,
                diagnostics=core,
                prompts=core,
                digests=core,
                skills=skills,
                pulls=pulls,
            )
            worker = TaskWorker(
                core=core,
                prompts=prompts,
                engine=engine,
                clock=clock,
                policy=s.worker.to_policy(),
                observer=tracing.observer() if s.tracing else None,
                workspaces=workspaces,
                skills=skills,
            )
            loop = asyncio.get_running_loop()
            for stop_signal in (signal.SIGTERM, signal.SIGINT):
                with contextlib.suppress(NotImplementedError):
                    loop.add_signal_handler(stop_signal, worker.stop)
            _log.info(
                "%s ready: core %s, prompts %s, skills %s, pull requests %s",
                SERVICE_NAME,
                s.core_url,
                s.langfuse_url,
                "on" if skills is not None else "off",
                "on" if pulls is not None else "off",
            )
            try:
                await worker.run_forever()
            finally:
                await prompts.aclose()
                tracing.shutdown()

    def _workspaces(
        self, config: ClaudeEngineConfig, clock: SystemClock
    ) -> SessionWorkspaces | None:
        s = self._settings.workspace
        if not s.enabled:
            return None
        wrap = (
            _sandboxed(config.sandbox, s.root, config.shell_prefix) if sandbox.available() else None
        )
        if wrap is None:
            _log.warning("Landlock is unavailable: shared-volume git runs unsandboxed")
        _log.info("shared volume at %s for %s", s.root, s.repository)
        return SessionWorkspaces(s.to_config(), SubprocessRunner(), clock, wrap=wrap)


def _sandboxed(
    rules: SandboxConfig, root: Path, shell: Path
) -> Callable[[Sequence[str]], list[str]]:
    """Run a shared-volume git command in the engine's sandbox shell: the volume's
    config is not trusted, so it must not reach the service's secrets (ADR-0037 §7)."""
    env = [
        f"{sandbox.ENV_MODE}={sandbox.LANDLOCK}",
        f"{sandbox.ENV_READ}={os.pathsep.join(str(p) for p in rules.read)}",
        f"{sandbox.ENV_WRITE}={os.pathsep.join([str(root), *SANDBOX_WRITE])}",
        f"{sandbox.ENV_SECRETS}={os.pathsep.join(str(p) for p in rules.secrets)}",
    ]

    def wrap(argv: Sequence[str]) -> list[str]:
        return ["env", *env, str(shell), shlex.join(argv)]

    return wrap


def _value(values: dict[CredentialName, SecretStr], name: CredentialName) -> SecretStr:
    value = values.get(name)
    if value is None:
        raise ConfigurationError(f"core did not return credential {name}")
    return value
