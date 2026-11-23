"""Composition root: fetches credentials from core, assembles and runs the worker."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
from typing import TYPE_CHECKING, Final

from enikk_contracts.credentials import CredentialName, CredentialRequest
from enikk_core_client import CoreClient
from enikk_core_client.credentials import wait_for_credentials
from enikk_core_client.tokens import FileTokenSource
from enikk_engine_claude.engine import ClaudeEngine, ClaudeEngineConfig, SandboxConfig
from enikk_kernel.clock import SystemClock
from enikk_kernel.errors import ConfigurationError
from enikk_langfuse.config import LangfuseConfig
from enikk_langfuse.prompts import LangfusePromptStore
from enikk_langfuse.tracing import LangfuseTracing
from enikk_runtime.worker import TaskWorker

if TYPE_CHECKING:
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
            engine = ClaudeEngine(
                config=ClaudeEngineConfig(
                    workdir=s.workdir,
                    scratch_dir=s.scratch_dir,
                    # The core token must stay out of the model's reach (ADR-0033).
                    sandbox=SandboxConfig(secrets=(s.token_file, s.token_file.parent)),
                ),
                oauth_token=_value(values, CredentialName.CLAUDE_OAUTH_TOKEN),
                transcripts=core,
                delegator=core,
                memory=core,
                diagnostics=core,
                prompts=core,
                digests=core,
            )
            worker = TaskWorker(
                core=core,
                prompts=prompts,
                engine=engine,
                clock=clock,
                policy=s.worker.to_policy(),
                observer=tracing.observer() if s.tracing else None,
            )
            loop = asyncio.get_running_loop()
            for stop_signal in (signal.SIGTERM, signal.SIGINT):
                with contextlib.suppress(NotImplementedError):
                    loop.add_signal_handler(stop_signal, worker.stop)
            _log.info("%s ready: core %s, prompts %s", SERVICE_NAME, s.core_url, s.langfuse_url)
            try:
                await worker.run_forever()
            finally:
                await prompts.aclose()
                tracing.shutdown()


def _value(values: dict[CredentialName, SecretStr], name: CredentialName) -> SecretStr:
    value = values.get(name)
    if value is None:
        raise ConfigurationError(f"core did not return credential {name}")
    return value
