"""``PromptRegistry`` over the Langfuse public API, for core (ADR-0032).

Endpoints (Langfuse OpenAPI, checked 2026-11-23):
- ``GET  /api/public/v2/prompts/{name}?label=|version=&resolve=false``
- ``POST /api/public/v2/prompts`` creates a new version of ``name``
- ``PATCH /api/public/v2/prompts/{name}/versions/{version}`` with ``newLabels``

A candidate is created with the ``candidate`` label alone; there is no parameter for
other labels. ``production`` is set by ``set_production`` only, which core calls for
the owner's promotion or revert (I-36).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Annotated, Final, Literal
from urllib.parse import quote

import httpx
from pydantic import Field, JsonValue, ValidationError

from enikk_core_domain.errors import DependencyUnavailableError, InvalidRequestError
from enikk_core_domain.prompts import CANDIDATE, PRODUCTION, StoredPrompt
from enikk_kernel.models import FrozenModel

if TYPE_CHECKING:
    from collections.abc import Callable

    from enikk_langfuse.config import LangfuseConfig

REQUEST_TIMEOUT_SECONDS: Final = 15.0
MISSING_KEYS: Final = (
    "core has no Langfuse API keys (langfuse.public-key, langfuse.secret-key); "
    "prompt candidates are off until the owner sets them with enikk-ops secret"
)

_log = logging.getLogger(__name__)


class _ApiPrompt(FrozenModel, extra="ignore"):
    name: str
    version: Annotated[int, Field(ge=1)]
    type: Literal["text", "chat"]
    prompt: str | list[JsonValue]
    config: JsonValue = None
    labels: tuple[str, ...] = ()

    def stored(self) -> StoredPrompt:
        return StoredPrompt(
            name=self.name,
            version=self.version,
            type=self.type,
            text=self.prompt if isinstance(self.prompt, str) else None,
            config=self.config if isinstance(self.config, dict) else {},
            labels=self.labels,
        )


class _CreateCandidate(FrozenModel):
    """``CreateTextPromptRequest``. ``labels`` can only be ``["candidate"]``; ``tags`` is
    left out because Langfuse applies tags to every version of the prompt."""

    name: str
    type: Literal["text"] = "text"
    prompt: str
    config: dict[str, JsonValue]
    labels: tuple[Literal["candidate"]] = (CANDIDATE,)
    commitMessage: str  # noqa: N815 - Langfuse's field name


class _NewLabels(FrozenModel):
    newLabels: tuple[Literal["production"]] = (PRODUCTION,)  # noqa: N815 - Langfuse's field name


class LangfusePromptRegistry:
    """Reads the keys on every call, so a ``secret sync`` applies without a restart."""

    def __init__(
        self,
        config: Callable[[], LangfuseConfig | None],
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._config = config
        self._transport = transport

    async def get(
        self, name: str, *, label: str | None = None, version: int | None = None
    ) -> StoredPrompt | None:
        params = {"resolve": "false"}
        if label is not None:
            params["label"] = label
        if version is not None:
            params["version"] = str(version)
        response = await self._send("GET", _prompt_path(name), params=params)
        if response.status_code == httpx.codes.NOT_FOUND:
            return None
        _raise_for(response, f"reading prompt '{name}'")
        return _parse(response.content, name)

    async def create_candidate(
        self, name: str, text: str, config: dict[str, JsonValue], message: str
    ) -> StoredPrompt:
        body = _CreateCandidate(name=name, prompt=text, config=config, commitMessage=message[:500])
        response = await self._send("POST", "/v2/prompts", content=body.model_dump_json())
        _raise_for(response, f"creating a candidate of '{name}'")
        created = _parse(response.content, name)
        if PRODUCTION in created.labels:
            # Never expected: the request had the candidate label alone.
            raise DependencyUnavailableError(f"Langfuse labelled candidate {name} production")
        _log.info("prompt %s: candidate version %d created", name, created.version)
        return created

    async def set_production(self, name: str, version: int) -> None:
        path = f"{_prompt_path(name)}/versions/{version}"
        response = await self._send("PATCH", path, content=_NewLabels().model_dump_json())
        _raise_for(response, f"labelling '{name}' v{version} production")
        _log.info("prompt %s: production is now version %d", name, version)

    async def _send(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str] | None = None,
        content: str | None = None,
    ) -> httpx.Response:
        config = self._config()
        if config is None:
            raise DependencyUnavailableError(MISSING_KEYS)
        async with httpx.AsyncClient(
            base_url=f"{config.base_url}/api/public",
            auth=httpx.BasicAuth(
                config.public_key.get_secret_value(), config.secret_key.get_secret_value()
            ),
            timeout=REQUEST_TIMEOUT_SECONDS,
            transport=self._transport,
        ) as http:
            try:
                return await http.request(
                    method,
                    path,
                    params=params,
                    content=content,
                    headers={"Content-Type": "application/json"} if content else None,
                )
            except httpx.HTTPError as exc:
                raise DependencyUnavailableError(
                    f"Langfuse is unreachable: {type(exc).__name__}"
                ) from exc


def _prompt_path(name: str) -> str:
    return f"/v2/prompts/{quote(name, safe='')}"


def _raise_for(response: httpx.Response, doing: str) -> None:
    if response.is_success:
        return
    if response.status_code in (httpx.codes.UNAUTHORIZED, httpx.codes.FORBIDDEN):
        raise DependencyUnavailableError(f"Langfuse rejected core's API keys while {doing}")
    if response.status_code == httpx.codes.BAD_REQUEST:
        raise InvalidRequestError(f"Langfuse refused {doing}: {response.text[:300]}")
    raise DependencyUnavailableError(f"Langfuse returned {response.status_code} while {doing}")


def _parse(body: bytes, name: str) -> StoredPrompt:
    try:
        return _ApiPrompt.model_validate_json(body).stored()
    except ValidationError as exc:
        raise DependencyUnavailableError(f"unexpected Langfuse response for '{name}'") from exc
