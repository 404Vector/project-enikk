"""Process entry point: ``enikk-imagegen``."""

from __future__ import annotations

import asyncio
import logging
import sys
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Final

import httpx
import uvicorn

from enikk_contracts.credentials import CredentialName, CredentialRequest
from enikk_core_client import CoreClient, FileTokenSource
from enikk_core_client.credentials import wait_for_credentials
from enikk_imagegen.app import create_app
from enikk_imagegen.files import Workspace
from enikk_imagegen.gemini import GeminiImages
from enikk_imagegen.service import ImageService, Limits, NoRecorder
from enikk_imagegen.settings import ImagegenSettings
from enikk_imagegen.tools import SERVER_NAME
from enikk_kernel.errors import EnikkError
from enikk_kernel.logging import configure_logging
from enikk_langfuse.config import LangfuseConfig
from enikk_langfuse.images import ImageTracing
from enikk_langfuse.styles import LangfuseStyles
from enikk_langfuse.tool_configs import LangfuseToolConfigs

if TYPE_CHECKING:
    from pydantic import SecretStr

    from enikk_imagegen.service import ImageRecorder

_log = logging.getLogger(__name__)

GEMINI: Final = CredentialRequest(names=(CredentialName.GEMINI_API_KEY,))
TRACING: Final = CredentialRequest(
    names=(
        CredentialName.GEMINI_API_KEY,
        CredentialName.LANGFUSE_PUBLIC_KEY,
        CredentialName.LANGFUSE_SECRET_KEY,
    )
)


def _now() -> datetime:
    return datetime.now(UTC)


def main() -> None:
    settings = ImagegenSettings()
    configure_logging(verbose=settings.verbose)
    tracing: ImageTracing | None = None
    try:
        request = TRACING if settings.tracing else GEMINI
        values = asyncio.run(_credentials(settings, request))
        recorder: ImageRecorder = NoRecorder()
        styles: LangfuseStyles | None = None
        configs: LangfuseToolConfigs | None = None
        if settings.tracing:
            langfuse = _langfuse(settings, values)
            tracing = ImageTracing.connect(langfuse, service_name=SERVER_NAME)
            recorder = tracing
            styles = LangfuseStyles.connect(langfuse, _now)
            configs = LangfuseToolConfigs.connect(langfuse, _now)
        http = httpx.AsyncClient(timeout=settings.request_seconds)
        service = ImageService(
            GeminiImages(http, settings.gemini_url, values[CredentialName.GEMINI_API_KEY]),
            Workspace(settings.workspace),
            Limits(
                default_budget_usd=settings.default_budget_usd,
                timezone=settings.timezone,
                default_model=settings.default_model,
                models=settings.models,
                prices=settings.prices,
            ),
            recorder=recorder,
            now=_now,
            styles=styles,
            settings=configs,
        )
        app = create_app(service, allowed_hosts=settings.allowed_hosts)
        _log.info(
            "starting %s port=%d model=%s default_budget_usd=%.2f",
            SERVER_NAME,
            settings.port,
            settings.default_model,
            settings.default_budget_usd,
        )
        uvicorn.run(app, host=settings.host, port=settings.port, log_config=None)
    except EnikkError as error:
        _log.error("error: %s", error)
        sys.exit(error.exit_code)
    finally:
        if tracing is not None:
            tracing.shutdown()


async def _credentials(
    settings: ImagegenSettings, request: CredentialRequest
) -> dict[CredentialName, SecretStr]:
    async with CoreClient.connect(settings.core_url, FileTokenSource(settings.token_file)) as core:
        return (await wait_for_credentials(core, request)).values


def _langfuse(
    settings: ImagegenSettings, values: dict[CredentialName, SecretStr]
) -> LangfuseConfig:
    return LangfuseConfig(
        base_url=settings.langfuse_url,
        public_key=values[CredentialName.LANGFUSE_PUBLIC_KEY],
        secret_key=values[CredentialName.LANGFUSE_SECRET_KEY],
    )
