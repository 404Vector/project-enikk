"""``LangfuseSkillLibrary`` (ADR-0037 §2, §3)."""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING

import httpx
import pytest
from pydantic import JsonValue, SecretStr

from enikk_contracts.skills import Capability, SkillSummary
from enikk_kernel.testing import ManualClock
from enikk_langfuse.config import LangfuseConfig
from enikk_langfuse.prompts import LangfusePromptStore
from enikk_runtime.errors import PromptInvalidError, PromptUnavailableError

if TYPE_CHECKING:
    from enikk_langfuse.skills import LangfuseSkillLibrary

pytestmark = pytest.mark.anyio

CONFIG = LangfuseConfig(
    base_url="https://jp.cloud.langfuse.com",
    public_key=SecretStr("pk-lf-test"),
    secret_key=SecretStr("sk-lf-test"),
)
GOOD: dict[str, JsonValue] = {
    "model": "claude-a",
    "description": "코드를 바꿀 때",
    "requires": ["pr.write"],
}


class _Langfuse:
    def __init__(self, prompts: dict[str, dict[str, JsonValue]]) -> None:
        self.prompts = prompts
        self.requests: list[httpx.Request] = []
        self.down = False

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.down:
            raise httpx.ConnectError("down")
        assert request.url.params["label"] == "production"
        path = request.url.path.removeprefix("/api/public")
        if path == "/v2/prompts":
            names = [{"name": n} for n in [*self.prompts, "assistant"]]
            return httpx.Response(200, json={"data": names, "meta": {"page": 1, "totalPages": 1}})
        name = path.removeprefix("/v2/prompts/")
        if name not in self.prompts:
            return httpx.Response(404, json={"message": "not found"})
        body: dict[str, JsonValue] = {"name": name, "version": 3, "type": "text", "prompt": "do"}
        body.update(self.prompts[name])
        return httpx.Response(200, json=body)


def _library(langfuse: _Langfuse, clock: ManualClock | None = None) -> LangfuseSkillLibrary:
    store = LangfusePromptStore.connect(
        CONFIG, clock or ManualClock(), transport=httpx.MockTransport(langfuse)
    )
    return store.skills()


async def test_load_returns_the_production_skill() -> None:
    skill = await _library(_Langfuse({"skill-dev": {"config": GOOD}})).load("skill-dev")
    assert (skill.name, skill.version, skill.text) == ("skill-dev", 3, "do")
    assert skill.requires == (Capability.PR_WRITE,)


@pytest.mark.parametrize(
    ("prompts", "name", "message"),
    [
        ({}, "skill-dev", "no production version"),
        ({}, "assistant", "not a skill name"),
        ({"skill-dev": {"config": {**GOOD, "requires": ["x"]}}}, "skill-dev", "requires"),
        ({"skill-dev": {"config": {"model": "m"}}}, "skill-dev", "description"),
        ({"skill-dev": {"config": GOOD, "type": "chat", "prompt": []}}, "skill-dev", "text"),
    ],
)
async def test_unusable_skills_are_refused(
    prompts: dict[str, dict[str, JsonValue]], name: str, message: str
) -> None:
    with pytest.raises(PromptInvalidError, match=message):
        await _library(_Langfuse(prompts)).load(name)


async def test_catalog_lists_valid_skills_only_and_is_cached() -> None:
    langfuse = _Langfuse({"skill-dev": {"config": GOOD}, "skill-bad": {"config": {"model": "m"}}})
    clock = ManualClock()
    library = _library(langfuse, clock)
    expected = (SkillSummary(name="skill-dev", description="코드를 바꿀 때"),)
    assert await library.catalog() == expected
    sent = len(langfuse.requests)
    assert await library.catalog() == expected
    assert len(langfuse.requests) == sent
    clock.advance(timedelta(minutes=2))
    langfuse.down = True
    with pytest.raises(PromptUnavailableError):
        await library.catalog()
    assert (await library.load("skill-dev")).version == 3
