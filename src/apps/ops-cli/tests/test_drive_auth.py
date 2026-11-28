from __future__ import annotations

import base64
import hashlib
from typing import TYPE_CHECKING
from urllib.parse import parse_qs, urlparse

import httpx
import pydantic_core
import pytest
from pydantic import SecretStr

from enikk_contracts.credentials import CredentialName
from enikk_kernel.errors import ConfigurationError
from enikk_kernel.testing import ManualClock
from enikk_ops.drive_auth import (
    SCOPE,
    TOKEN_URL,
    DriveAuth,
    DriveAuthError,
    authorization_url,
    pkce_pair,
)
from enikk_ops.secrets import SecretFile

if TYPE_CHECKING:
    from pathlib import Path

CLIENT_ID = CredentialName.DRIVE_OAUTH_CLIENT_ID
CLIENT_SECRET = CredentialName.DRIVE_OAUTH_CLIENT_SECRET
REFRESH = CredentialName.DRIVE_OAUTH_REFRESH_TOKEN


def _file(tmp_path: Path, *, client: bool = True) -> SecretFile:
    file = SecretFile(tmp_path / "cfg" / "secrets.yaml", ManualClock())
    if client:
        file.set(CLIENT_ID, SecretStr("cid.apps.googleusercontent.com"))
        file.set(CLIENT_SECRET, SecretStr("csecret"))
    return file


def _google(seen: list[httpx.Request], reply: dict[str, object], status: int = 200) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(status, content=pydantic_core.to_json(reply))

    return httpx.Client(transport=httpx.MockTransport(handler))


def _browser(answer: dict[str, str] | None = None) -> tuple[list[str], object]:
    """Plays the owner's browser: follows Google's redirect to the local listener."""
    opened: list[str] = []

    def open_browser(url: str) -> None:
        opened.append(url)
        query = {key: values[0] for key, values in parse_qs(urlparse(url).query).items()}
        params = answer if answer is not None else {"code": "c-1", "state": query["state"]}
        httpx.get(query["redirect_uri"], params=params)

    return opened, open_browser


def test_pkce_challenge_is_the_s256_of_the_verifier() -> None:
    verifier, challenge = pkce_pair()
    digest = hashlib.sha256(verifier.encode()).digest()
    assert challenge == base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


def test_authorization_url_asks_for_offline_drive_access() -> None:
    url = authorization_url("cid", "http://127.0.0.1:5/", "chal", "st")
    query = {key: values[0] for key, values in parse_qs(urlparse(url).query).items()}
    assert query["scope"] == SCOPE
    assert query["access_type"] == "offline"
    assert query["prompt"] == "consent"
    assert query["code_challenge_method"] == "S256"
    assert query["redirect_uri"] == "http://127.0.0.1:5/"


def test_consent_saves_the_refresh_token_without_printing_it(tmp_path: Path) -> None:
    file = _file(tmp_path)
    seen: list[httpx.Request] = []
    said: list[str] = []
    opened, open_browser = _browser()
    with _google(seen, {"access_token": "at", "refresh_token": "rt-9"}) as http:
        DriveAuth(file, http, open_browser=open_browser, say=said.append).run()  # type: ignore[arg-type]
    assert file.values()[REFRESH].get_secret_value() == "rt-9"
    assert str(seen[0].url) == TOKEN_URL
    form = parse_qs(seen[0].content.decode())
    assert form["code"] == ["c-1"]
    assert form["grant_type"] == ["authorization_code"]
    assert "code_verifier" in form
    assert not any("rt-9" in line or "csecret" in line for line in said)
    assert len(opened) == 1


def test_a_wrong_state_saves_nothing(tmp_path: Path) -> None:
    file = _file(tmp_path)
    seen: list[httpx.Request] = []
    _, open_browser = _browser({"code": "c-1", "state": "forged"})
    with (
        _google(seen, {"refresh_token": "rt"}) as http,
        pytest.raises(DriveAuthError, match="wrong state"),
    ):
        DriveAuth(file, http, open_browser=open_browser, say=lambda _: None).run()  # type: ignore[arg-type]
    assert seen == []
    assert REFRESH not in file.values()


def test_a_reply_without_a_refresh_token_is_an_error(tmp_path: Path) -> None:
    file = _file(tmp_path)
    _, open_browser = _browser()
    with (
        _google([], {"access_token": "at"}) as http,
        pytest.raises(DriveAuthError, match="no refresh token"),
    ):
        DriveAuth(file, http, open_browser=open_browser, say=lambda _: None).run()  # type: ignore[arg-type]


def test_the_client_must_be_set_first(tmp_path: Path) -> None:
    with (
        httpx.Client() as http,
        pytest.raises(ConfigurationError, match=r"drive\.oauth-client-id"),
    ):
        DriveAuth(_file(tmp_path, client=False), http, open_browser=print, say=print).run()
