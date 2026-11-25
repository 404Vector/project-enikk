"""One image request: the style, the daily limit, the model call, the file and the
trace. And the styles themselves: their list and reference images."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, Protocol
from zoneinfo import ZoneInfo

from enikk_imagegen.gemini import GeminiError
from enikk_kernel.errors import EnikkError
from enikk_kernel.models import FrozenModel
from enikk_langfuse.images import ImageRecord
from enikk_langfuse.styles import StyleError

if TYPE_CHECKING:
    import uuid
    from collections.abc import Callable, Sequence
    from datetime import date, datetime

    from enikk_imagegen.files import Workspace
    from enikk_imagegen.gemini import GeneratedImage, InlineImage
    from enikk_langfuse.styles import Style

_log = logging.getLogger(__name__)

MAX_IMAGES: Final = 14
"""Images one request may carry: inputs and the style's references (ADR-0043)."""


class LimitReachedError(EnikkError):
    """Today's images are used up."""


class ImageModel(Protocol):
    async def generate(
        self,
        model: str,
        prompt: str,
        images: Sequence[InlineImage],
        *,
        aspect_ratio: str | None,
        image_size: str | None,
    ) -> GeneratedImage: ...


class StyleSource(Protocol):
    async def get(self, name: str) -> Style: ...

    async def available(self) -> tuple[Style, ...]: ...


class NoStyles:
    async def get(self, name: str) -> Style:
        raise StyleError("styles are off: this server has no Langfuse connection")

    async def available(self) -> tuple[Style, ...]:
        return ()


class ImageRecorder(Protocol):
    def record(self, record: ImageRecord) -> None: ...


class NoRecorder:
    def record(self, record: ImageRecord) -> None:
        pass


class ImageResult(FrozenModel):
    path: str
    model: str
    image_size: str
    aspect_ratio: str | None
    estimated_cost_usd: float | None
    used_today: int
    daily_limit: int
    model_text: str
    style: str | None = None
    style_version: int | None = None


class StyleSummary(FrozenModel):
    name: str
    description: str
    model: str
    version: int
    references: list[str]


@dataclass(frozen=True)
class Limits:
    daily_limit: int
    timezone: str
    default_model: str
    models: tuple[str, ...]
    prices: dict[str, dict[str, float]]


class DailyCount:
    """Images started today, in memory. A restart starts the count again (ADR-0043)."""

    def __init__(self, limit: int, timezone: str, now: Callable[[], datetime]) -> None:
        self._limit = limit
        self._zone = ZoneInfo(timezone)
        self._now = now
        self._day: date | None = None
        self._used = 0
        self._lock = asyncio.Lock()

    def _today(self) -> date:
        today = self._now().astimezone(self._zone).date()
        if today != self._day:
            self._day, self._used = today, 0
        return today

    async def take(self) -> int:
        async with self._lock:
            self._today()
            if self._used >= self._limit:
                raise LimitReachedError(
                    f"today's limit of {self._limit} images is used up; it resets at midnight "
                    f"{self._zone.key}"
                )
            self._used += 1
            return self._used

    async def give_back(self) -> None:
        async with self._lock:
            self._today()
            self._used = max(0, self._used - 1)

    @property
    def limit(self) -> int:
        return self._limit


class ImageService:
    def __init__(
        self,
        model: ImageModel,
        workspace: Workspace,
        limits: Limits,
        *,
        recorder: ImageRecorder,
        now: Callable[[], datetime],
        styles: StyleSource | None = None,
    ) -> None:
        self._styles: StyleSource = styles or NoStyles()
        self._model = model
        self._workspace = workspace
        self._limits = limits
        self._recorder = recorder
        self._now = now
        self._count = DailyCount(limits.daily_limit, limits.timezone, now)

    def model_for(self, requested: str | None) -> str:
        model = requested or self._limits.default_model
        allowed = {self._limits.default_model, *self._limits.models}
        if model not in allowed:
            raise EnikkError(f"model {model} is not allowed; use one of {sorted(allowed)}")
        return model

    async def make(
        self,
        *,
        tool: str,
        session: uuid.UUID,
        prompt: str,
        inputs: Sequence[str],
        name: str,
        model: str | None,
        aspect_ratio: str | None,
        image_size: str,
        style: str | None = None,
    ) -> ImageResult:
        applied = await self._style(style)
        chosen = self.model_for(model or (applied.model if applied else None))
        images = [self._workspace.read(session, path) for path in inputs]
        if applied is not None:
            images += self._workspace.style_references(applied.name)
            prompt = f"{applied.text.strip()}\n\n{prompt}"
        if len(images) > MAX_IMAGES:
            raise EnikkError(
                f"{len(images)} images (inputs and style references); at most {MAX_IMAGES}"
            )
        used = await self._count.take()
        cost = self._limits.prices.get(chosen, {}).get(image_size)
        parameters = {"image_size": image_size, "aspect_ratio": aspect_ratio or "auto"}
        try:
            made = await self._model.generate(
                chosen, prompt, images, aspect_ratio=aspect_ratio, image_size=image_size
            )
        except GeminiError as exc:
            await self._count.give_back()
            self._trace(
                tool, session, prompt, chosen, parameters, len(images), applied, error=str(exc)
            )
            raise
        path = self._workspace.save(session, made.image, name=name, now=self._now())
        self._trace(
            tool,
            session,
            prompt,
            chosen,
            parameters,
            len(images),
            applied,
            made=made,
            cost=cost,
            output=str(path),
        )
        _log.info("image saved model=%s size=%s used_today=%d", chosen, image_size, used)
        return ImageResult(
            path=str(path),
            model=chosen,
            image_size=image_size,
            aspect_ratio=aspect_ratio,
            estimated_cost_usd=cost,
            used_today=used,
            daily_limit=self._count.limit,
            model_text=made.text,
            style=applied.name if applied else None,
            style_version=applied.version if applied else None,
        )

    async def _style(self, name: str | None) -> Style | None:
        return await self._require(name) if name else None

    async def _require(self, name: str) -> Style:
        try:
            return await self._styles.get(name)
        except StyleError as exc:
            raise EnikkError(str(exc)) from exc

    async def styles(self) -> list[StyleSummary]:
        return [await self._summary(style) for style in await self._styles.available()]

    async def add_reference(self, session: uuid.UUID, style: str, image: str) -> StyleSummary:
        """Keep one of the thread's images as a reference of an existing style."""
        found = await self._require(style)
        self._workspace.add_style_reference(session, found.name, image)
        return await self._summary(found)

    async def remove_reference(self, style: str, file_name: str) -> StyleSummary:
        found = await self._require(style)
        self._workspace.remove_style_reference(found.name, file_name)
        return await self._summary(found)

    async def _summary(self, style: Style) -> StyleSummary:
        return StyleSummary(
            name=style.name,
            description=style.description,
            model=style.model,
            version=style.version,
            references=self._workspace.style_reference_names(style.name),
        )

    def _trace(
        self,
        tool: str,
        session: uuid.UUID,
        prompt: str,
        model: str,
        parameters: dict[str, str],
        references: int,
        style: Style | None,
        *,
        made: GeneratedImage | None = None,
        cost: float | None = None,
        output: str | None = None,
        error: str | None = None,
    ) -> None:
        record = ImageRecord(
            session_id=session,
            tool=tool,
            prompt=prompt,
            model=model,
            parameters=parameters,
            references=references,
            input_tokens=made.input_tokens if made else None,
            output_tokens=made.output_tokens if made else None,
            cost_usd=cost if made else None,
            output=output,
            error=error,
            style=style.name if style else None,
            style_version=style.version if style else None,
        )
        try:
            self._recorder.record(record)
        except Exception:
            _log.exception("could not record the image trace")
