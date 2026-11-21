from __future__ import annotations

import base64
from datetime import timedelta
from typing import TYPE_CHECKING

import httpx
import pytest
from pydantic import JsonValue, SecretStr

from enikk_contracts.agents import PromptRef
from enikk_kernel.testing import ManualClock
from enikk_langfuse.config import LangfuseConfig, OtlpExport
from enikk_langfuse.prompts import CachePolicy, LangfusePromptStore
from enikk_runtime.errors import PromptInvalidError, PromptUnavailableError

if TYPE_CHECKING:
    from collections.abc import Callable

pytestmark = pytest.mark.anyio

CONFIG = LangfuseConfig(
    base_url="https://jp.cloud.langfuse.com",
    public_key=SecretStr("pk-lf-test"),
    secret_key=SecretStr("sk-lf-test"),
)
PRODUCTION = PromptRef(name="assistant", label="production")


def _text_prompt(**overrides: JsonValue) -> dict[str, JsonValue]:
    body: dict[str, JsonValue] = {
        "name": "assistant",
        "version": 2,
        "type": "text",
        "prompt": "You are enikk.",
        "config": {"model": "claude-opus-5-5", "temperature": 0.2},
        "labels": ["production", "latest"],
        "tags": [],
    }
    body.update(overrides)
    return body


class _Langfuse:
    def __init__(self, respond: Callable[[httpx.Request], httpx.Response]) -> None:
        self.requests: list[httpx.Request] = []
        self._respond = respond

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self._respond(request)


def _store(
    langfuse: _Langfuse, clock: ManualClock | None = None, cache: CachePolicy | None = None
) -> LangfusePromptStore:
    return LangfusePromptStore.connect(
        CONFIG, clock or ManualClock(), cache, transport=httpx.MockTransport(langfuse)
    )


def _ok(**overrides: JsonValue) -> Callable[[httpx.Request], httpx.Response]:
    return lambda _: httpx.Response(200, json=_text_prompt(**overrides))


async def test_fetches_a_text_prompt_by_label() -> None:
    langfuse = _Langfuse(_ok())
    prompt = await _store(langfuse).get(PRODUCTION)
    assert prompt.name == "assistant"
    assert prompt.version == 2
    assert prompt.text == "You are enikk."
    assert prompt.model == "claude-opus-5-5"
    assert prompt.parameters == {"temperature": 0.2}

    [request] = langfuse.requests
    assert request.url.path == "/api/public/v2/prompts/assistant"
    assert request.url.params["label"] == "production"
    expected = base64.b64encode(b"pk-lf-test:sk-lf-test").decode()
    assert request.headers["Authorization"] == f"Basic {expected}"


async def test_version_selector_and_folder_names_are_encoded() -> None:
    langfuse = _Langfuse(_ok(name="team/assistant"))
    await _store(langfuse).get(PromptRef(name="team/assistant", version=7))
    [request] = langfuse.requests
    assert request.url.raw_path.startswith(b"/api/public/v2/prompts/team%2Fassistant")
    assert request.url.params["version"] == "7"


async def test_fresh_cache_avoids_requests_until_it_expires() -> None:
    clock = ManualClock()
    langfuse = _Langfuse(_ok())
    store = _store(langfuse, clock)
    await store.get(PRODUCTION)
    await store.get(PRODUCTION)
    assert len(langfuse.requests) == 1
    clock.advance(timedelta(seconds=61))
    await store.get(PRODUCTION)
    assert len(langfuse.requests) == 2


async def test_stale_copy_is_used_only_while_langfuse_is_down() -> None:
    clock = ManualClock()
    healthy = True

    def respond(_: httpx.Request) -> httpx.Response:
        if healthy:
            return httpx.Response(200, json=_text_prompt())
        return httpx.Response(503)

    store = _store(_Langfuse(respond), clock)
    first = await store.get(PRODUCTION)
    healthy = False
    clock.advance(timedelta(minutes=30))
    assert await store.get(PRODUCTION) == first
    clock.advance(timedelta(minutes=31))
    with pytest.raises(PromptUnavailableError):
        await store.get(PRODUCTION)


async def test_unreachable_langfuse_without_cache_is_retryable() -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    with pytest.raises(PromptUnavailableError) as raised:
        await _store(_Langfuse(refuse)).get(PRODUCTION)
    assert raised.value.retryable


@pytest.mark.parametrize(
    ("respond", "message"),
    [
        (lambda _: httpx.Response(404), "does not exist"),
        (lambda _: httpx.Response(401), "rejected"),
        (_ok(type="chat", prompt=[{"role": "system", "content": "x"}]), "text prompt"),
        (_ok(config={"temperature": 1}), 'no "model"'),
        (_ok(config=None), 'no "model"'),
        (lambda _: httpx.Response(200, text="not json"), "unexpected"),
    ],
)
async def test_unusable_prompts_are_not_retried(
    respond: Callable[[httpx.Request], httpx.Response], message: str
) -> None:
    with pytest.raises(PromptInvalidError, match=message) as raised:
        await _store(_Langfuse(respond)).get(PRODUCTION)
    assert not raised.value.retryable
    assert "sk-lf-test" not in raised.value.reason


def test_otlp_export_targets_langfuse_with_basic_auth() -> None:
    export = OtlpExport.for_langfuse(CONFIG)
    env = export.environment()
    assert env["OTEL_EXPORTER_OTLP_ENDPOINT"] == "https://jp.cloud.langfuse.com/api/public/otel"
    assert env["OTEL_EXPORTER_OTLP_PROTOCOL"] == "http/protobuf"
    token = base64.b64encode(b"pk-lf-test:sk-lf-test").decode()
    assert env["OTEL_EXPORTER_OTLP_HEADERS"] == (
        f"Authorization=Basic {token},x-langfuse-ingestion-version=4"
    )
    assert "sk-lf-test" not in repr(export)
    assert token not in repr(export)


def test_base_url_must_be_a_bare_host() -> None:
    with pytest.raises(ValueError, match="base_url"):
        LangfuseConfig(
            base_url="https://jp.cloud.langfuse.com/api/public",
            public_key=SecretStr("p"),
            secret_key=SecretStr("s"),
        )
