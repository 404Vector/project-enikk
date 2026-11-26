"""One image request: the style, the daily budget, the model call, the file and the
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
from enikk_langfuse.tool_configs import ToolConfigError

if TYPE_CHECKING:
    import uuid
    from collections.abc import Callable, Sequence
    from datetime import date, datetime

    from enikk_contracts.tool_configs import ImagegenConfig
    from enikk_imagegen.files import Workspace
    from enikk_imagegen.gemini import GeneratedImage, InlineImage
    from enikk_langfuse.styles import Style

_log = logging.getLogger(__name__)

MAX_IMAGES: Final = 14
"""Images one request may carry: inputs and the style's references (ADR-0043)."""


class LimitReachedError(EnikkError):
    """Today's budget is used up."""


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


class SettingSource(Protocol):
    async def imagegen(self) -> ImagegenConfig | None: ...


class NoSettings:
    async def imagegen(self) -> ImagegenConfig | None:
        return None


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
    estimated_cost_usd: float
    spent_today_usd: float
    daily_budget_usd: float
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
    default_budget_usd: float
    """The daily budget while ``config-imagegen`` has no production version."""
    timezone: str
    default_model: str
    models: tuple[str, ...]
    prices: dict[str, dict[str, float]]


class DailySpend:
    """Estimated USD spent today, in memory. A restart starts again from 0 (ADR-0045)."""

    def __init__(self, timezone: str, now: Callable[[], datetime]) -> None:
        self._zone = ZoneInfo(timezone)
        self._now = now
        self._day: date | None = None
        self._spent = 0.0
        self._lock = asyncio.Lock()

    def _today(self) -> None:
        today = self._now().astimezone(self._zone).date()
        if today != self._day:
            self._day, self._spent = today, 0.0

    async def take(self, cost: float, budget: float) -> float:
        """Count ``cost`` for today and return today's total; refuse past ``budget``."""
        async with self._lock:
            self._today()
            if self._spent + cost > budget + 1e-9:
                raise LimitReachedError(
                    f"today's budget of ${budget:.2f} would be exceeded "
                    f"(${self._spent:.3f} spent, this image ${cost:.3f}); it resets at "
                    f"midnight {self._zone.key}"
                )
            self._spent += cost
            return self._spent

    async def give_back(self, cost: float) -> None:
        async with self._lock:
            self._today()
            self._spent = max(0.0, self._spent - cost)


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
        settings: SettingSource | None = None,
    ) -> None:
        self._styles: StyleSource = styles or NoStyles()
        self._settings: SettingSource = settings or NoSettings()
        self._model = model
        self._workspace = workspace
        self._limits = limits
        self._recorder = recorder
        self._now = now
        self._spend = DailySpend(limits.timezone, now)

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
        cost = self._limits.prices.get(chosen, {}).get(image_size)
        if cost is None:
            raise EnikkError(
                f"no estimated price for {chosen} at size {image_size}, so the daily budget "
                "cannot be kept; use another size or model"
            )
        budget = await self.daily_budget_usd()
        spent = await self._spend.take(cost, budget)
        parameters = {"image_size": image_size, "aspect_ratio": aspect_ratio or "auto"}
        try:
            made = await self._model.generate(
                chosen, prompt, images, aspect_ratio=aspect_ratio, image_size=image_size
            )
        except GeminiError as exc:
            await self._spend.give_back(cost)
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
        _log.info(
            "image saved model=%s size=%s spent_today=%.3f budget=%.2f",
            chosen,
            image_size,
            spent,
            budget,
        )
        return ImageResult(
            path=str(path),
            model=chosen,
            image_size=image_size,
            aspect_ratio=aspect_ratio,
            estimated_cost_usd=cost,
            spent_today_usd=round(spent, 4),
            daily_budget_usd=budget,
            model_text=made.text,
            style=applied.name if applied else None,
            style_version=applied.version if applied else None,
        )

    async def daily_budget_usd(self) -> float:
        """Today's budget: ``config-imagegen`` in Langfuse, or the default while it has
        no production version. Refuses when the setting cannot be read."""
        try:
            found = await self._settings.imagegen()
        except ToolConfigError as exc:
            raise EnikkError(
                f"the daily budget cannot be read, so no image is made: {exc}"
            ) from exc
        return self._limits.default_budget_usd if found is None else found.daily_budget_usd

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
