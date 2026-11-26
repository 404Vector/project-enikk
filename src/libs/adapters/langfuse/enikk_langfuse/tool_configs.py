"""Tool settings (``config-*``) over the Langfuse public API (ADR-0046).

Only ``production`` versions are read. A cached copy serves for ``fresh``; when
Langfuse cannot be reached, one up to ``stale_limit`` old. A setting with no
production version is ``None``; the caller decides its default.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import TYPE_CHECKING, Annotated, Final, Literal, Self
from urllib.parse import quote

import httpx
from pydantic import Field, JsonValue, ValidationError

from enikk_contracts.tool_configs import (
    IMAGEGEN_CONFIG,
    TOOL_CONFIGS,
    ImagegenConfig,
    tool_config,
)
from enikk_kernel.errors import EnikkError
from enikk_kernel.models import FrozenModel

if TYPE_CHECKING:
    from collections.abc import Callable
    from datetime import datetime

    from enikk_contracts.base import Contract
    from enikk_langfuse.config import LangfuseConfig

PRODUCTION: Final = "production"

_log = logging.getLogger(__name__)


class ToolConfigError(EnikkError):
    """A tool setting is invalid, or Langfuse cannot be reached for it."""


class _ApiPrompt(FrozenModel, extra="ignore"):
    version: Annotated[int, Field(ge=1)]
    type: Literal["text", "chat"]
    config: JsonValue = None


class LangfuseToolConfigs:
    def __init__(
        self,
        http: httpx.AsyncClient,
        now: Callable[[], datetime],
        *,
        fresh: timedelta = timedelta(seconds=60),
        stale_limit: timedelta = timedelta(hours=1),
    ) -> None:
        self._http = http
        self._now = now
        self._fresh = fresh
        self._stale_limit = stale_limit
        self._cache: dict[str, tuple[Contract | None, datetime]] = {}

    @classmethod
    def connect(
        cls, config: LangfuseConfig, now: Callable[[], datetime], *, timeout: float = 10.0
    ) -> Self:
        http = httpx.AsyncClient(
            base_url=f"{config.base_url}/api/public",
            headers={"Authorization": f"Basic {config.basic_token().get_secret_value()}"},
            timeout=timeout,
        )
        return cls(http, now)

    async def aclose(self) -> None:
        await self._http.aclose()

    async def imagegen(self) -> ImagegenConfig | None:
        found = await self.get(IMAGEGEN_CONFIG)
        if found is not None and not isinstance(found, ImagegenConfig):
            raise ToolConfigError(f"{IMAGEGEN_CONFIG} has an unexpected contract")
        return found

    async def get(self, name: str) -> Contract | None:
        if name not in TOOL_CONFIGS:
            raise ToolConfigError(f"'{name}' is not a tool setting")
        now = self._now()
        cached = self._cache.get(name)
        if cached is not None and now - cached[1] < self._fresh:
            return cached[0]
        try:
            found = await self._fetch(name)
        except _UnreachableError as exc:
            if cached is not None and now - cached[1] < self._stale_limit:
                _log.warning("Langfuse unreachable; using cached setting %s", name)
                return cached[0]
            raise ToolConfigError(str(exc)) from exc
        self._cache[name] = (found, now)
        return found

    async def _fetch(self, name: str) -> Contract | None:
        path = f"/v2/prompts/{quote(name, safe='')}"
        try:
            response = await self._http.get(path, params={"label": PRODUCTION})
        except httpx.HTTPError as exc:
            raise _UnreachableError(
                f"Langfuse is unreachable for setting '{name}': {type(exc).__name__}"
            ) from exc
        if response.status_code == httpx.codes.NOT_FOUND:
            return None
        if response.status_code in (httpx.codes.UNAUTHORIZED, httpx.codes.FORBIDDEN):
            raise ToolConfigError("Langfuse rejected the server's API keys")
        if not response.is_success:
            raise _UnreachableError(
                f"Langfuse returned {response.status_code} for setting '{name}'"
            )
        try:
            api = _ApiPrompt.model_validate_json(response.content)
        except ValidationError as exc:
            raise ToolConfigError(f"unexpected Langfuse response for setting '{name}'") from exc
        try:
            return tool_config(name, api.config if isinstance(api.config, dict) else {})
        except ValueError as exc:
            raise ToolConfigError(f"setting '{name}' v{api.version}: {exc}") from exc


class _UnreachableError(Exception):
    pass
