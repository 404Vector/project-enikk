from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pydantic_core
import pytest

from enikk_contracts.tool_configs import ImagegenConfig, tool_config
from enikk_langfuse.tool_configs import LangfuseToolConfigs, ToolConfigError

pytestmark = pytest.mark.anyio

SETTING = {
    "name": "config-imagegen",
    "version": 3,
    "type": "text",
    "prompt": "하루 예산",
    "config": {"daily_budget_usd": 1},
}


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 11, 26, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now


def _configs(handler: httpx.MockTransport, clock: Clock) -> LangfuseToolConfigs:
    http = httpx.AsyncClient(transport=handler, base_url="https://lf.test/api/public")
    return LangfuseToolConfigs(http, clock)


def _ok(body: object) -> httpx.Response:
    return httpx.Response(200, content=pydantic_core.to_json(body))


async def test_the_production_setting_is_read() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return _ok(SETTING)

    found = await _configs(httpx.MockTransport(handler), Clock()).imagegen()
    assert found == ImagegenConfig(daily_budget_usd=1.0)
    assert seen[0].url.path == "/api/public/v2/prompts/config-imagegen"
    assert seen[0].url.params["label"] == "production"


async def test_a_missing_setting_is_none() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    assert await _configs(httpx.MockTransport(handler), Clock()).imagegen() is None


async def test_an_invalid_setting_is_an_error() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return _ok({**SETTING, "config": {"daily_budget_usd": "lots"}})

    with pytest.raises(ToolConfigError, match="v3"):
        await _configs(httpx.MockTransport(handler), Clock()).imagegen()


async def test_a_cached_setting_serves_while_langfuse_is_down_then_errors() -> None:
    up = True

    def handler(_: httpx.Request) -> httpx.Response:
        if not up:
            raise httpx.ConnectError("down")
        return _ok(SETTING)

    clock = Clock()
    configs = _configs(httpx.MockTransport(handler), clock)
    await configs.imagegen()
    up = False
    clock.now += timedelta(minutes=5)
    assert await configs.imagegen() == ImagegenConfig(daily_budget_usd=1.0)
    clock.now += timedelta(hours=2)
    with pytest.raises(ToolConfigError, match="unreachable"):
        await configs.imagegen()


def test_only_known_settings_exist() -> None:
    with pytest.raises(ValueError, match="not a tool setting"):
        tool_config("config-other", {})
    with pytest.raises(ValueError, match="invalid config"):
        tool_config("config-imagegen", {"daily_budget_usd": 1, "extra": True})
