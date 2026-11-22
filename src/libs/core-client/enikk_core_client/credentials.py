"""Waiting for the credentials a service needs at startup (ADR-0013)."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Final

from enikk_core_client.errors import CoreNotFoundError, CoreUnavailableError

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from enikk_contracts.credentials import CredentialRequest, Credentials
    from enikk_core_client.client import CoreClient

    Sleep = Callable[[float], Awaitable[None]]

RETRY_MAX_SECONDS: Final = 60.0

_log = logging.getLogger(__name__)


async def wait_for_credentials(
    core: CoreClient, request: CredentialRequest, sleep: Sleep = asyncio.sleep
) -> Credentials:
    """Retry with backoff until core is reachable and every requested credential is set."""
    delay = 1.0
    while True:
        try:
            return await core.fetch_credentials(request)
        except CoreUnavailableError as exc:
            _log.warning("core unavailable while fetching credentials: %s", exc)
        except CoreNotFoundError as exc:
            _log.warning("%s; set them with `enikk-ops secret set` and `secret sync`", exc)
        await sleep(delay)
        delay = min(delay * 2, RETRY_MAX_SECONDS)
