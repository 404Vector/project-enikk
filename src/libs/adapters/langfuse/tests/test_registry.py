"""Langfuse prompt writes for core (ADR-0032), against a fake Langfuse over HTTP."""

from __future__ import annotations

import base64

import httpx
import pydantic_core
import pytest
from pydantic import SecretStr

from enikk_core_domain.errors import DependencyUnavailableError, InvalidRequestError
from enikk_langfuse.config import LangfuseConfig
from enikk_langfuse.registry import MISSING_KEYS, LangfusePromptRegistry

pytestmark = pytest.mark.anyio

CONFIG = LangfuseConfig(
    base_url="https://jp.cloud.langfuse.com",
    public_key=SecretStr("pk-lf-test"),
    secret_key=SecretStr("sk-lf-test"),
)


def _prompt(version: int, labels: list[str], text: str = "hello") -> dict[str, object]:
    return {
        "name": "assistant",
        "version": version,
        "type": "text",
        "prompt": text,
        "config": {"model": "claude-a"},
        "labels": labels,
        "tags": [],
    }


class FakeLangfuse:
    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if request.method == "GET" and path == "/api/public/v2/prompts/assistant":
            if request.url.params.get("label") == "missing":
                return httpx.Response(404, json={"message": "not found"})
            return httpx.Response(200, json=_prompt(3, ["production", "latest"]))
        if request.method == "POST" and path == "/api/public/v2/prompts":
            body = pydantic_core.from_json(request.content)
            return httpx.Response(200, json=_prompt(4, body["labels"] + ["latest"], body["prompt"]))
        if request.method == "PATCH" and path == "/api/public/v2/prompts/assistant/versions/4":
            return httpx.Response(200, json=_prompt(4, ["production"]))
        return httpx.Response(500)


def _registry(fake: FakeLangfuse, config: LangfuseConfig | None = CONFIG) -> LangfusePromptRegistry:
    return LangfusePromptRegistry(lambda: config, transport=httpx.MockTransport(fake.handler))


async def test_reads_the_stored_version_with_basic_auth() -> None:
    fake = FakeLangfuse()
    found = await _registry(fake).get("assistant", label="production")
    assert found is not None
    assert (found.version, found.text, found.config) == (3, "hello", {"model": "claude-a"})
    [request] = fake.requests
    assert request.url.params["label"] == "production"
    assert request.url.params["resolve"] == "false"
    expected = base64.b64encode(b"pk-lf-test:sk-lf-test").decode()
    assert request.headers["Authorization"] == f"Basic {expected}"


async def test_a_missing_version_is_none() -> None:
    assert await _registry(FakeLangfuse()).get("assistant", label="missing") is None


async def test_candidate_is_created_with_the_candidate_label_alone() -> None:
    fake = FakeLangfuse()
    created = await _registry(fake).create_candidate(
        "assistant", "new text", {"model": "claude-b"}, "shorter answers"
    )
    assert created.version == 4
    [request] = fake.requests
    body = pydantic_core.from_json(request.content)
    assert body == {
        "name": "assistant",
        "type": "text",
        "prompt": "new text",
        "config": {"model": "claude-b"},
        "labels": ["candidate"],
        "commitMessage": "shorter answers",
    }
    assert "tags" not in body
    assert all(r.method != "PATCH" for r in fake.requests)


async def test_production_is_moved_by_its_own_call_only() -> None:
    fake = FakeLangfuse()
    await _registry(fake).set_production("assistant", 4)
    [request] = fake.requests
    assert request.method == "PATCH"
    assert pydantic_core.from_json(request.content) == {"newLabels": ["production"]}


async def test_without_keys_the_feature_is_off_with_a_clear_error() -> None:
    fake = FakeLangfuse()
    with pytest.raises(DependencyUnavailableError, match="no Langfuse API keys"):
        await _registry(fake, None).get("assistant", label="production")
    assert fake.requests == []
    assert "enikk-ops secret" in MISSING_KEYS


@pytest.mark.parametrize(
    ("status", "error", "message"),
    [
        (401, DependencyUnavailableError, "rejected core's API keys"),
        (400, InvalidRequestError, "Langfuse refused"),
        (503, DependencyUnavailableError, "returned 503"),
    ],
)
async def test_errors_are_reported(status: int, error: type[Exception], message: str) -> None:
    registry = LangfusePromptRegistry(
        lambda: CONFIG,
        transport=httpx.MockTransport(lambda _: httpx.Response(status, text="bad")),
    )
    with pytest.raises(error, match=message):
        await registry.create_candidate("assistant", "x", {"model": "m"}, "why")


async def test_unreachable_langfuse() -> None:
    def fail(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down", request=request)

    registry = LangfusePromptRegistry(lambda: CONFIG, transport=httpx.MockTransport(fail))
    with pytest.raises(DependencyUnavailableError, match="unreachable"):
        await registry.set_production("assistant", 4)
