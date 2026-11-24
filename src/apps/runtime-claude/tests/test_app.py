from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING

import httpx
import pytest
from pydantic import SecretStr

from enikk_contracts.credentials import CredentialName, CredentialRequest, Credentials
from enikk_contracts.errors import ErrorCode, ErrorResponse
from enikk_core_client import CoreClient, StaticTokenSource
from enikk_core_client.credentials import wait_for_credentials
from enikk_engine_claude import sandbox
from enikk_engine_claude.engine import SandboxConfig
from enikk_runtime_claude.app import CREDENTIALS, _sandboxed
from enikk_runtime_claude.settings import RuntimeClaudeSettings

if TYPE_CHECKING:
    from collections.abc import Iterator

pytestmark = pytest.mark.anyio

SETTINGS_PREFIX = "ENIKK_RUNTIME_"


@pytest.fixture(autouse=True)
def _without_service_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """Removes the runtime's own settings from the environment for every test."""
    for name in list(os.environ):
        if name.startswith(SETTINGS_PREFIX):
            monkeypatch.delenv(name)


def _responses() -> Iterator[httpx.Response]:
    yield httpx.Response(503, text="starting")
    yield httpx.Response(
        404,
        content=ErrorResponse(
            code=ErrorCode.NOT_FOUND, message="credentials not set: claude.oauth-token"
        ).model_dump_json(),
    )
    values = {name: SecretStr(f"value-{name.value}") for name in CREDENTIALS.names}
    yield httpx.Response(200, content=Credentials(values=values).model_dump_json())


async def test_credentials_are_fetched_once_core_has_them() -> None:
    responses = _responses()
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return next(responses)

    pauses: list[float] = []

    async def pause(seconds: float) -> None:
        pauses.append(seconds)

    client = CoreClient.connect(
        "http://core.test",
        StaticTokenSource(SecretStr("token")),
        transport=httpx.MockTransport(handler),
    )
    async with client:
        credentials = await wait_for_credentials(client, CREDENTIALS, sleep=pause)

    assert pauses == [1.0, 2.0]
    token = credentials.values[CredentialName.CLAUDE_OAUTH_TOKEN]
    assert token.get_secret_value() == "value-claude.oauth-token"
    sent = CredentialRequest.model_validate_json(requests[-1].content)
    assert set(sent.names) == {
        CredentialName.CLAUDE_OAUTH_TOKEN,
        CredentialName.LANGFUSE_PUBLIC_KEY,
        CredentialName.LANGFUSE_SECRET_KEY,
    }


def test_defaults_point_at_the_cluster_and_the_japan_region() -> None:
    settings = RuntimeClaudeSettings()
    assert settings.core_url == "http://enikk-core.enikk.svc"
    assert settings.langfuse_url == "https://jp.cloud.langfuse.com"
    assert settings.worker.to_policy().wait_seconds == 20
    assert settings.worker.to_policy().slots == 1


def test_the_chart_sets_the_number_of_slots(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENIKK_RUNTIME_WORKER__SLOTS", "2")
    assert RuntimeClaudeSettings().worker.to_policy().slots == 2


def test_the_shared_volume_is_off_unless_the_chart_turns_it_on() -> None:
    settings = RuntimeClaudeSettings()
    assert not settings.workspace.enabled
    config = settings.workspace.to_config()
    assert config.root == Path("/data")
    assert config.remote_url == "https://github.com/404Vector/project-enikk.git"
    assert config.idle_days == 7


def test_shared_volume_git_runs_in_the_sandbox_shell_without_the_secrets() -> None:
    rules = SandboxConfig(secrets=(Path("/run/secrets/token"),))
    wrap = _sandboxed(rules, Path("/data"), Path("/app/.venv/bin/enikk-sandbox-shell"))
    argv = wrap(["git", "--git-dir=/data/repo.git", "fetch", "origin"])

    assert argv[0] == "env"
    assert f"{sandbox.ENV_MODE}={sandbox.LANDLOCK}" in argv
    assert f"{sandbox.ENV_SECRETS}=/run/secrets/token" in argv
    write = next(a for a in argv if a.startswith(f"{sandbox.ENV_WRITE}="))
    assert write.split("=", 1)[1].split(os.pathsep) == ["/data", "/dev"]
    assert argv[-2] == "/app/.venv/bin/enikk-sandbox-shell"
    assert argv[-1] == "git --git-dir=/data/repo.git fetch origin"
