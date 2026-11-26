"""Process entry point: ``enikk-unity``."""

from __future__ import annotations

import asyncio
import logging
import sys
from typing import TYPE_CHECKING, Final

import httpx
import uvicorn

from enikk_contracts.credentials import CredentialName, CredentialRequest
from enikk_core_client import CoreClient, FileTokenSource
from enikk_core_client.credentials import wait_for_credentials
from enikk_kernel.errors import EnikkError
from enikk_kernel.logging import configure_logging
from enikk_unity.app import create_app
from enikk_unity.relay import SERVER_NAME, Relay
from enikk_unity.settings import UnitySettings
from enikk_unity.upstream import UnityUpstream

if TYPE_CHECKING:
    from pydantic import SecretStr

_log = logging.getLogger(__name__)

KEY: Final = CredentialRequest(names=(CredentialName.UNITY_API_KEY,))


def main() -> None:
    settings = UnitySettings()
    configure_logging(verbose=settings.verbose)
    try:
        key = asyncio.run(_key(settings))
        timeout = httpx.Timeout(settings.request_seconds, connect=settings.connect_seconds)
        http = httpx.AsyncClient(timeout=timeout)
        upstream = UnityUpstream(
            http, settings.upstream_url, key, protocol_version=settings.protocol_version
        )
        app = create_app(
            Relay(upstream, instance=settings.instance), allowed_hosts=settings.allowed_hosts
        )
        _log.info(
            "starting %s port=%d upstream=%s instance=%s",
            SERVER_NAME,
            settings.port,
            settings.upstream_url,
            settings.instance or "-",
        )
        uvicorn.run(app, host=settings.host, port=settings.port, log_config=None)
    except EnikkError as error:
        _log.error("error: %s", error)
        sys.exit(error.exit_code)


async def _key(settings: UnitySettings) -> SecretStr:
    async with CoreClient.connect(settings.core_url, FileTokenSource(settings.token_file)) as core:
        values = (await wait_for_credentials(core, KEY)).values
    return values[CredentialName.UNITY_API_KEY]
