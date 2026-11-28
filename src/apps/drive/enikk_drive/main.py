"""Process entry point: ``enikk-drive``."""

from __future__ import annotations

import asyncio
import logging
import sys
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Final

import httpx
import uvicorn

from enikk_contracts.credentials import CredentialName, CredentialRequest
from enikk_contracts.routes import MCP_JWKS
from enikk_core_client import CoreClient, FileTokenSource
from enikk_core_client.credentials import wait_for_credentials
from enikk_drive.app import create_app
from enikk_drive.drive_api import DriveApi, OAuthToken
from enikk_drive.files import Workspace
from enikk_drive.service import ExchangeService
from enikk_drive.settings import DriveSettings
from enikk_drive.tokens import TokenVerifier
from enikk_drive.tools import SERVER_NAME
from enikk_kernel.errors import EnikkError
from enikk_kernel.logging import configure_logging

if TYPE_CHECKING:
    from pydantic import SecretStr

_log = logging.getLogger(__name__)

GOOGLE: Final = CredentialRequest(
    names=(
        CredentialName.DRIVE_OAUTH_CLIENT_ID,
        CredentialName.DRIVE_OAUTH_CLIENT_SECRET,
        CredentialName.DRIVE_OAUTH_REFRESH_TOKEN,
    )
)


def _now() -> datetime:
    return datetime.now(UTC)


def main() -> None:
    settings = DriveSettings()
    configure_logging(verbose=settings.verbose)
    try:
        values = asyncio.run(_credentials(settings))
        http = httpx.AsyncClient(timeout=settings.request_seconds)
        token = OAuthToken(
            http,
            settings.oauth_token_url,
            client_id=values[CredentialName.DRIVE_OAUTH_CLIENT_ID],
            client_secret=values[CredentialName.DRIVE_OAUTH_CLIENT_SECRET],
            refresh_token=values[CredentialName.DRIVE_OAUTH_REFRESH_TOKEN],
            now=_now,
        )
        service = ExchangeService(
            DriveApi(http, token, api_url=settings.api_url, upload_url=settings.upload_url),
            Workspace(settings.workspace),
            root_folder=settings.root_folder,
            max_bytes=settings.max_file_mb * 1024 * 1024,
        )
        verifier = TokenVerifier(http, settings.core_url.rstrip("/") + MCP_JWKS)
        app = create_app(service, verifier, allowed_hosts=settings.allowed_hosts)
        _log.info(
            "starting %s port=%d root_folder=%s max_file_mb=%d",
            SERVER_NAME,
            settings.port,
            settings.root_folder,
            settings.max_file_mb,
        )
        uvicorn.run(app, host=settings.host, port=settings.port, log_config=None)
    except EnikkError as error:
        _log.error("error: %s", error)
        sys.exit(error.exit_code)


async def _credentials(settings: DriveSettings) -> dict[CredentialName, SecretStr]:
    async with CoreClient.connect(settings.core_url, FileTokenSource(settings.token_file)) as core:
        return (await wait_for_credentials(core, GOOGLE)).values
