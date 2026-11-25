from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pydantic_core
import pytest

from enikk_contracts.styles import style_config, valid_style_name
from enikk_langfuse.styles import LangfuseStyles, StyleError

pytestmark = pytest.mark.anyio

MODEL = "gemini-3.1-flash-image"
STYLE = {
    "name": "style-pixel",
    "version": 2,
    "type": "text",
    "prompt": "16-bit pixel art.",
    "config": {"model": MODEL, "description": "sprites"},
}


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 11, 25, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now


def _styles(handler: httpx.MockTransport, clock: Clock) -> LangfuseStyles:
    http = httpx.AsyncClient(transport=handler, base_url="https://lf.test/api/public")
    return LangfuseStyles(http, clock)


def _ok(body: object) -> httpx.Response:
    return httpx.Response(200, content=pydantic_core.to_json(body))


async def test_a_style_is_read_from_its_production_version() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return _ok(STYLE)

    styles = _styles(httpx.MockTransport(handler), Clock())
    style = await styles.get("style-pixel")
    assert (style.version, style.text, style.model) == (2, "16-bit pixel art.", MODEL)
    assert seen[0].url.path == "/api/public/v2/prompts/style-pixel"
    assert seen[0].url.params["label"] == "production"


async def test_the_list_holds_only_valid_styles() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/v2/prompts"):
            names = [{"name": "enikk"}, {"name": "style-pixel"}, {"name": "style-broken"}]
            return _ok({"data": names, "meta": {"totalPages": 1}})
        if request.url.path.endswith("style-broken"):
            return _ok({**STYLE, "name": "style-broken", "config": {"model": "m"}})
        return _ok(STYLE)

    listed = await _styles(httpx.MockTransport(handler), Clock()).available()
    assert [style.name for style in listed] == ["style-pixel"]


async def test_a_cached_style_serves_while_langfuse_is_down() -> None:
    up = True

    def handler(_: httpx.Request) -> httpx.Response:
        if not up:
            raise httpx.ConnectError("down")
        return _ok(STYLE)

    clock = Clock()
    styles = _styles(httpx.MockTransport(handler), clock)
    await styles.get("style-pixel")
    up = False
    clock.now += timedelta(minutes=5)
    assert (await styles.get("style-pixel")).version == 2
    clock.now += timedelta(hours=1)
    with pytest.raises(StyleError, match="unreachable"):
        await styles.get("style-pixel")


async def test_missing_and_misnamed_styles_are_errors() -> None:
    styles = _styles(httpx.MockTransport(lambda _: httpx.Response(404)), Clock())
    with pytest.raises(StyleError, match="no production version"):
        await styles.get("style-none")
    with pytest.raises(StyleError, match="not a style name"):
        await styles.get("skill-developer")


def test_style_names_and_config() -> None:
    assert valid_style_name("style-pixel-2")
    assert not valid_style_name("style-Pixel")
    assert style_config({"model": "m", "description": "d"}).model == "m"
    with pytest.raises(ValueError, match="description"):
        style_config({"model": "m"})
