from __future__ import annotations

from typing import TYPE_CHECKING

import httpx
import pytest
from pydantic import SecretStr

from enikk_contracts.credentials import CredentialName, CredentialRequest, Credentials
from enikk_contracts.errors import ErrorCode, ErrorResponse
from enikk_core_client import CoreClient, StaticTokenSource
from enikk_core_client.credentials import wait_for_credentials
from enikk_runtime_claude.app import CREDENTIALS
from enikk_runtime_claude.settings import RuntimeClaudeSettings

if TYPE_CHECKING:
    from collections.abc import Iterator

pytestmark = pytest.mark.anyio


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
