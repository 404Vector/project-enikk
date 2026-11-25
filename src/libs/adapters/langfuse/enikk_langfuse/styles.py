"""Image styles (``style-*``) over the Langfuse public API (ADR-0043 §3).

Only ``production`` versions are read. A cached copy serves for ``fresh``; when
Langfuse cannot be reached, one up to ``stale_limit`` old. Nothing is made up.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import TYPE_CHECKING, Annotated, Final, Literal, Self
from urllib.parse import quote

import httpx
from pydantic import Field, JsonValue, ValidationError

from enikk_contracts.styles import (
    MAX_STYLES,
    STYLE_PREFIX,
    style_config,
    valid_style_name,
)
from enikk_kernel.errors import EnikkError
from enikk_kernel.models import FrozenModel

if TYPE_CHECKING:
    from collections.abc import Callable
    from datetime import datetime

    from enikk_langfuse.config import LangfuseConfig

PRODUCTION: Final = "production"
PAGE_SIZE: Final = 100
MAX_PAGES: Final = 5

_log = logging.getLogger(__name__)


class StyleError(EnikkError):
    """A style is missing, invalid, or Langfuse cannot be reached for it."""


class Style(FrozenModel):
    name: str
    version: int
    text: str
    model: str
    description: str


class _ApiPrompt(FrozenModel, extra="ignore"):
    name: str
    version: Annotated[int, Field(ge=1)]
    type: Literal["text", "chat"]
    prompt: str | list[JsonValue]
    config: JsonValue = None


class _ListedPrompt(FrozenModel, extra="ignore"):
    name: str


class _ListMeta(FrozenModel, extra="ignore"):
    totalPages: int = 1  # noqa: N815 - Langfuse's field name


class _PromptList(FrozenModel, extra="ignore"):
    data: tuple[_ListedPrompt, ...] = ()
    meta: _ListMeta = _ListMeta()


class LangfuseStyles:
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
        self._cache: dict[str, tuple[Style, datetime]] = {}

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

    async def get(self, name: str) -> Style:
        if not valid_style_name(name):
            raise StyleError(f"'{name}' is not a style name (style-…)")
        now = self._now()
        cached = self._cache.get(name)
        if cached is not None and now - cached[1] < self._fresh:
            return cached[0]
        try:
            style = await self._fetch(name)
        except _UnreachableError as exc:
            if cached is not None and now - cached[1] < self._stale_limit:
                _log.warning("Langfuse unreachable; using cached style %s", name)
                return cached[0]
            raise StyleError(str(exc)) from exc
        self._cache[name] = (style, now)
        return style

    async def available(self) -> tuple[Style, ...]:
        """The styles with a valid production version, by name."""
        found: list[Style] = []
        for name in (await self._names())[:MAX_STYLES]:
            try:
                found.append(await self.get(name))
            except StyleError as exc:
                _log.warning("style %s is not listed: %s", name, exc)
        return tuple(found)

    async def _names(self) -> list[str]:
        names: list[str] = []
        page = 1
        while page <= MAX_PAGES:
            params = {"label": PRODUCTION, "limit": str(PAGE_SIZE), "page": str(page)}
            try:
                body = await self._get("/v2/prompts", params, "the prompt list")
                listed = _PromptList.model_validate_json(body)
            except _UnreachableError as exc:
                raise StyleError(str(exc)) from exc
            except ValidationError as exc:
                raise StyleError("unexpected Langfuse prompt list") from exc
            names += [p.name for p in listed.data if p.name.startswith(STYLE_PREFIX)]
            if page >= listed.meta.totalPages:
                break
            page += 1
        return sorted(set(names))

    async def _fetch(self, name: str) -> Style:
        path = f"/v2/prompts/{quote(name, safe='')}"
        body = await self._get(path, {"label": PRODUCTION}, f"style '{name}'")
        try:
            api = _ApiPrompt.model_validate_json(body)
        except ValidationError as exc:
            raise StyleError(f"unexpected Langfuse response for style '{name}'") from exc
        if api.type != "text" or not isinstance(api.prompt, str) or not api.prompt.strip():
            raise StyleError(f"style '{name}' must be a non-empty text prompt")
        try:
            config = style_config(api.config if isinstance(api.config, dict) else {})
        except ValueError as exc:
            raise StyleError(f"style '{name}': {exc}") from exc
        return Style(
            name=name,
            version=api.version,
            text=api.prompt,
            model=config.model,
            description=config.description,
        )

    async def _get(self, path: str, params: dict[str, str], what: str) -> bytes:
        try:
            response = await self._http.get(path, params=params)
        except httpx.HTTPError as exc:
            raise _UnreachableError(
                f"Langfuse is unreachable for {what}: {type(exc).__name__}"
            ) from exc
        if response.status_code == httpx.codes.NOT_FOUND:
            raise StyleError(f"{what} has no production version in Langfuse")
        if response.status_code in (httpx.codes.UNAUTHORIZED, httpx.codes.FORBIDDEN):
            raise StyleError("Langfuse rejected the image server's API keys")
        if not response.is_success:
            raise _UnreachableError(f"Langfuse returned {response.status_code} for {what}")
        return response.content


class _UnreachableError(Exception):
    pass
