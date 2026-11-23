"""``SkillLibrary`` over the Langfuse public API (ADR-0037 §2, §3).

A skill is a text prompt named ``skill-*``; its ``production`` version is the one
sessions load. Its config must be a valid ``SkillConfig``.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Annotated, Final, Literal
from urllib.parse import quote

import httpx
from pydantic import Field, JsonValue, ValidationError

from enikk_contracts.skills import (
    MAX_SKILLS,
    SKILL_PREFIX,
    SkillSummary,
    skill_config,
    valid_skill_name,
)
from enikk_kernel.models import FrozenModel
from enikk_runtime.errors import PromptInvalidError, PromptUnavailableError
from enikk_runtime.model import LoadedSkill

if TYPE_CHECKING:
    from datetime import datetime

    from enikk_kernel.clock import Clock
    from enikk_langfuse.prompts import CachePolicy

PRODUCTION: Final = "production"
PAGE_SIZE: Final = 100
MAX_PAGES: Final = 5

_log = logging.getLogger(__name__)


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


class LangfuseSkillLibrary:
    """Lists and loads skills. Serves a cached copy for ``cache.fresh``; never makes up a
    skill when Langfuse cannot be reached."""

    def __init__(self, http: httpx.AsyncClient, clock: Clock, cache: CachePolicy) -> None:
        self._http = http
        self._clock = clock
        self._cache = cache
        self._skills: dict[str, tuple[LoadedSkill, datetime]] = {}
        self._catalog: tuple[tuple[SkillSummary, ...], datetime] | None = None

    async def catalog(self) -> tuple[SkillSummary, ...]:
        now = self._clock.now()
        if self._catalog is not None and now - self._catalog[1] < self._cache.fresh:
            return self._catalog[0]
        summaries: list[SkillSummary] = []
        for name in (await self._names())[:MAX_SKILLS]:
            try:
                skill = await self.load(name)
            except PromptInvalidError as exc:
                _log.warning("skill %s is not listed: %s", name, exc.reason)
                continue
            summaries.append(SkillSummary(name=skill.name, description=skill.description))
        listed = tuple(summaries)
        self._catalog = (listed, now)
        return listed

    async def load(self, name: str) -> LoadedSkill:
        if not valid_skill_name(name):
            raise PromptInvalidError(f"'{name}' is not a skill name (skill-…)")
        now = self._clock.now()
        cached = self._skills.get(name)
        if cached is not None and now - cached[1] < self._cache.fresh:
            return cached[0]
        try:
            skill = await self._fetch(name)
        except PromptUnavailableError:
            if cached is not None and now - cached[1] < self._cache.stale_limit:
                _log.warning("Langfuse unreachable; using cached skill %s", name)
                return cached[0]
            raise
        self._skills[name] = (skill, now)
        return skill

    async def _names(self) -> list[str]:
        names: list[str] = []
        page = 1
        while page <= MAX_PAGES:
            params = {"label": PRODUCTION, "limit": str(PAGE_SIZE), "page": str(page)}
            body = await self._get("/v2/prompts", params, "the prompt list")
            try:
                listed = _PromptList.model_validate_json(body)
            except ValidationError as exc:
                raise PromptUnavailableError("unexpected Langfuse prompt list") from exc
            names += [p.name for p in listed.data if p.name.startswith(SKILL_PREFIX)]
            if page >= listed.meta.totalPages:
                break
            page += 1
        return sorted(set(names))

    async def _fetch(self, name: str) -> LoadedSkill:
        path = f"/v2/prompts/{quote(name, safe='')}"
        body = await self._get(path, {"label": PRODUCTION}, f"skill '{name}'")
        try:
            api = _ApiPrompt.model_validate_json(body)
        except ValidationError as exc:
            raise PromptInvalidError(f"unexpected Langfuse response for skill '{name}'") from exc
        if api.type != "text" or not isinstance(api.prompt, str) or not api.prompt.strip():
            raise PromptInvalidError(f"skill '{name}' must be a non-empty text prompt")
        try:
            config = skill_config(api.config if isinstance(api.config, dict) else {})
        except ValueError as exc:
            raise PromptInvalidError(f"skill '{name}': {exc}") from exc
        return LoadedSkill(
            name=name,
            version=api.version,
            text=api.prompt,
            description=config.description,
            requires=config.requires,
        )

    async def _get(self, path: str, params: dict[str, str], what: str) -> bytes:
        try:
            response = await self._http.get(path, params=params)
        except httpx.HTTPError as exc:
            raise PromptUnavailableError(
                f"Langfuse is unreachable for {what}: {type(exc).__name__}"
            ) from exc
        if response.status_code == httpx.codes.NOT_FOUND:
            raise PromptInvalidError(f"{what} has no production version in Langfuse")
        if response.status_code in (httpx.codes.UNAUTHORIZED, httpx.codes.FORBIDDEN):
            raise PromptInvalidError("Langfuse rejected the runtime's API keys")
        if not response.is_success:
            raise PromptUnavailableError(f"Langfuse returned {response.status_code} for {what}")
        return response.content
