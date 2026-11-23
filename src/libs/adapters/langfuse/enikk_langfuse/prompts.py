"""``PromptStore`` over the Langfuse public API, with a short cache (ADR-0016)."""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import TYPE_CHECKING, Annotated, Final, Literal, Self
from urllib.parse import quote

import httpx
from pydantic import AwareDatetime, Field, JsonValue, ValidationError

from enikk_kernel.models import FrozenModel
from enikk_langfuse.skills import LangfuseSkillLibrary
from enikk_runtime.errors import PromptInvalidError, PromptUnavailableError
from enikk_runtime.model import ResolvedPrompt

if TYPE_CHECKING:
    from enikk_contracts.agents import PromptRef
    from enikk_kernel.clock import Clock
    from enikk_langfuse.config import LangfuseConfig

MODEL_KEY: Final = "model"
REQUEST_TIMEOUT_SECONDS: Final = 10.0

_log = logging.getLogger(__name__)


class CachePolicy(FrozenModel):
    fresh: timedelta = timedelta(seconds=60)
    """Serve from cache without asking Langfuse."""
    stale_limit: timedelta = timedelta(hours=1)
    """Serve an older copy only when Langfuse cannot be reached."""


class _ApiPrompt(FrozenModel, extra="ignore"):
    name: str
    version: Annotated[int, Field(ge=1)]
    type: Literal["text", "chat"]
    prompt: str | list[JsonValue]
    config: JsonValue = None


class _Cached(FrozenModel):
    prompt: ResolvedPrompt
    fetched_at: AwareDatetime


class LangfusePromptStore:
    """Fetches text prompts. Never substitutes a default prompt (I-13)."""

    def __init__(self, http: httpx.AsyncClient, clock: Clock, cache: CachePolicy) -> None:
        self._http = http
        self._clock = clock
        self._cache_policy = cache
        self._cache: dict[tuple[str, str | None, int | None], _Cached] = {}

    @classmethod
    def connect(
        cls,
        config: LangfuseConfig,
        clock: Clock,
        cache: CachePolicy | None = None,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> Self:
        http = httpx.AsyncClient(
            base_url=f"{config.base_url}/api/public",
            auth=httpx.BasicAuth(
                config.public_key.get_secret_value(), config.secret_key.get_secret_value()
            ),
            timeout=REQUEST_TIMEOUT_SECONDS,
            transport=transport,
        )
        return cls(http, clock, cache or CachePolicy())

    async def aclose(self) -> None:
        await self._http.aclose()

    def skills(self) -> LangfuseSkillLibrary:
        """Skills (``skill-*``) over the same connection (ADR-0037 §3)."""
        return LangfuseSkillLibrary(self._http, self._clock, self._cache_policy)

    async def get(self, ref: PromptRef) -> ResolvedPrompt:
        key = (ref.name, ref.label, ref.version)
        now = self._clock.now()
        cached = self._cache.get(key)
        if cached is not None and now - cached.fetched_at < self._cache_policy.fresh:
            return cached.prompt
        try:
            prompt = await self._fetch(ref)
        except PromptUnavailableError:
            if cached is not None and now - cached.fetched_at < self._cache_policy.stale_limit:
                _log.warning("Langfuse unreachable; using cached prompt %s", _describe(ref))
                return cached.prompt
            raise
        self._cache[key] = _Cached(prompt=prompt, fetched_at=now)
        return prompt

    async def _fetch(self, ref: PromptRef) -> ResolvedPrompt:
        params: dict[str, str] = {}
        if ref.label is not None:
            params["label"] = ref.label
        if ref.version is not None:
            params["version"] = str(ref.version)
        path = f"/v2/prompts/{quote(ref.name, safe='')}"
        try:
            response = await self._http.get(path, params=params)
        except httpx.HTTPError as exc:
            raise PromptUnavailableError(
                f"Langfuse is unreachable for prompt {_describe(ref)}: {type(exc).__name__}"
            ) from exc
        if response.status_code == httpx.codes.NOT_FOUND:
            raise PromptInvalidError(f"prompt {_describe(ref)} does not exist in Langfuse")
        if response.status_code in (httpx.codes.UNAUTHORIZED, httpx.codes.FORBIDDEN):
            raise PromptInvalidError("Langfuse rejected the runtime's API keys")
        if not response.is_success:
            raise PromptUnavailableError(
                f"Langfuse returned {response.status_code} for prompt {_describe(ref)}"
            )
        return _resolve(ref, response.content)


def _resolve(ref: PromptRef, body: bytes) -> ResolvedPrompt:
    try:
        api = _ApiPrompt.model_validate_json(body)
    except ValidationError as exc:
        raise PromptInvalidError(f"unexpected Langfuse response for {_describe(ref)}") from exc
    if api.type != "text" or not isinstance(api.prompt, str):
        raise PromptInvalidError(f"prompt {_describe(ref)} must be a text prompt")
    config = api.config if isinstance(api.config, dict) else {}
    model = config.get(MODEL_KEY)
    if not isinstance(model, str) or not model:
        raise PromptInvalidError(f'prompt {_describe(ref)} has no "model" in its config')
    try:
        return ResolvedPrompt(
            name=api.name,
            version=api.version,
            text=api.prompt,
            model=model,
            parameters={k: v for k, v in config.items() if k != MODEL_KEY},
        )
    except ValidationError as exc:
        raise PromptInvalidError(f"prompt {_describe(ref)} has an invalid field") from exc


def _describe(ref: PromptRef) -> str:
    selector = f"label {ref.label}" if ref.label is not None else f"version {ref.version}"
    return f"'{ref.name}' ({selector})"
