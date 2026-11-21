from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from pydantic import ValidationError

from enikk_contracts.credentials import CredentialName, CredentialRequest, Credentials
from enikk_core.credentials import MAX_VALUE_BYTES, DirectoryCredentialStore
from enikk_core_client import CoreForbiddenError, CoreNotFoundError
from enikk_kernel.errors import ConfigurationError

if TYPE_CHECKING:
    from pathlib import Path

    from conftest import Harness

SLACK_TOKENS = CredentialRequest(
    names=(CredentialName.SLACK_BOT_TOKEN, CredentialName.SLACK_APP_TOKEN)
)


@pytest.mark.anyio
async def test_connector_receives_its_granted_credentials(harness: Harness) -> None:
    async with harness.client("slack") as client:
        credentials = await client.fetch_credentials(SLACK_TOKENS)
    assert credentials.values[CredentialName.SLACK_BOT_TOKEN].get_secret_value() == "xoxb-test"
    assert credentials.values[CredentialName.SLACK_APP_TOKEN].get_secret_value() == "xapp-test"
    assert "xoxb-test" not in repr(credentials)


@pytest.mark.anyio
async def test_response_is_not_cacheable(harness: Harness) -> None:
    async with harness.client("slack") as client:
        response = await client._http.post(
            "/v1/credentials",
            content=SLACK_TOKENS.model_dump_json(),
            headers={"Content-Type": "application/json"},
        )
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"


@pytest.mark.anyio
async def test_credentials_of_another_role_are_forbidden(harness: Harness) -> None:
    async with harness.client("claude") as client:
        with pytest.raises(CoreForbiddenError, match=r"slack\.bot-token"):
            await client.fetch_credentials(SLACK_TOKENS)


@pytest.mark.anyio
async def test_unbound_caller_is_forbidden(harness: Harness) -> None:
    async with harness.client("stranger") as client:
        with pytest.raises(CoreForbiddenError):
            await client.fetch_credentials(SLACK_TOKENS)


@pytest.mark.anyio
async def test_granted_but_unset_credential_is_not_found(harness: Harness) -> None:
    request = CredentialRequest(names=(CredentialName.CLAUDE_OAUTH_TOKEN,))
    async with harness.client("claude") as client:
        with pytest.raises(CoreNotFoundError, match=r"claude\.oauth-token"):
            await client.fetch_credentials(request)


def test_request_rejects_duplicate_names() -> None:
    name = CredentialName.SLACK_BOT_TOKEN
    with pytest.raises(ValidationError, match="unique"):
        CredentialRequest(names=(name, name))


def test_values_are_clear_only_in_json() -> None:
    credentials = Credentials.model_validate_json('{"values": {"slack.bot-token": "xoxb-1"}}')
    assert "xoxb-1" in credentials.model_dump_json()
    assert "xoxb-1" not in repr(credentials)
    assert "xoxb-1" not in str(credentials.model_dump())


def test_directory_store_reads_mounted_files(tmp_path: Path) -> None:
    (tmp_path / "slack.bot-token").write_text("xoxb-1\n")
    (tmp_path / "slack.app-token").write_text("  \n")
    store = DirectoryCredentialStore(tmp_path)
    value = store.get(CredentialName.SLACK_BOT_TOKEN)
    assert value is not None
    assert value.get_secret_value() == "xoxb-1"
    assert store.get(CredentialName.SLACK_APP_TOKEN) is None
    assert store.get(CredentialName.CLAUDE_OAUTH_TOKEN) is None


def test_directory_store_rejects_oversized_values(tmp_path: Path) -> None:
    (tmp_path / "slack.bot-token").write_text("x" * (MAX_VALUE_BYTES + 1))
    with pytest.raises(ConfigurationError, match="exceeds"):
        DirectoryCredentialStore(tmp_path).get(CredentialName.SLACK_BOT_TOKEN)
