"""The Drive server's refresh token from the owner's consent (ADR-0053 §5).

Installed-app OAuth with PKCE and a loopback redirect: the browser asks the owner to
allow Drive access, Google redirects to a one-shot local listener, and the code is
exchanged for a refresh token that goes straight into the credentials file. The token
is never printed.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import TYPE_CHECKING, Final
from urllib.parse import parse_qs, urlencode, urlparse

import httpx
from pydantic import BaseModel, ConfigDict, SecretStr, ValidationError

from enikk_contracts.credentials import CredentialName
from enikk_kernel.errors import ConfigurationError, EnikkError

if TYPE_CHECKING:
    from collections.abc import Callable

    from enikk_ops.secrets import SecretFile

AUTH_URL: Final = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL: Final = "https://oauth2.googleapis.com/token"  # noqa: S105 - a URL
SCOPE: Final = "https://www.googleapis.com/auth/drive"
WAIT_SECONDS: Final = 300.0
_PAGE: Final = "<html><body><p>enikk: {message} You can close this window.</p></body></html>"


class DriveAuthError(EnikkError):
    """The consent did not end in a refresh token."""


class _TokenReply(BaseModel):
    model_config = ConfigDict(extra="ignore")
    refresh_token: str | None = None
    error: str | None = None
    error_description: str | None = None


def pkce_pair() -> tuple[str, str]:
    """A PKCE verifier and its S256 challenge."""
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return verifier, challenge


def authorization_url(client_id: str, redirect_uri: str, challenge: str, state: str) -> str:
    query = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": SCOPE,
        "access_type": "offline",
        "prompt": "consent",
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": state,
    }
    return f"{AUTH_URL}?{urlencode(query)}"


class LoopbackReceiver:
    """Listens once on 127.0.0.1 for Google's redirect and keeps its query."""

    def __init__(self) -> None:
        self.params: dict[str, str] = {}
        received = self.params

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                query = parse_qs(urlparse(self.path).query)
                received.update({key: values[0] for key, values in query.items()})
                done = "code" in received
                message = "Drive access was granted." if done else "Access was not granted."
                body = _PAGE.format(message=message).encode()
                self.send_response(200)
                self.send_header("content-type", "text/html; charset=utf-8")
                self.send_header("content-length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, format: str, *args: object) -> None:  # noqa: A002
                return

        self._server = HTTPServer(("127.0.0.1", 0), Handler)
        self._server.timeout = WAIT_SECONDS

    @property
    def redirect_uri(self) -> str:
        return f"http://127.0.0.1:{self._server.server_address[1]}/"

    def start(self) -> threading.Thread:
        thread = threading.Thread(target=self._server.handle_request, daemon=True)
        thread.start()
        return thread

    def close(self) -> None:
        self._server.server_close()


def exchange_code(
    http: httpx.Client,
    *,
    code: str,
    client_id: SecretStr,
    client_secret: SecretStr,
    redirect_uri: str,
    verifier: str,
) -> SecretStr:
    form = {
        "grant_type": "authorization_code",
        "code": code,
        "client_id": client_id.get_secret_value(),
        "client_secret": client_secret.get_secret_value(),
        "redirect_uri": redirect_uri,
        "code_verifier": verifier,
    }
    try:
        response = http.post(TOKEN_URL, data=form)
    except httpx.HTTPError as exc:
        raise DriveAuthError(f"token exchange failed: {type(exc).__name__}") from exc
    try:
        reply = _TokenReply.model_validate_json(response.content)
    except ValidationError as exc:
        raise DriveAuthError(f"token exchange: unexpected response {response.status_code}") from exc
    if response.status_code != 200 or reply.error:
        detail = " ".join(p for p in (reply.error, reply.error_description) if p)
        raise DriveAuthError(f"token exchange: {response.status_code} {detail}".rstrip())
    if not reply.refresh_token:
        raise DriveAuthError(
            "Google returned no refresh token; remove enikk's access at "
            "https://myaccount.google.com/permissions and run this again"
        )
    return SecretStr(reply.refresh_token)


class DriveAuth:
    def __init__(
        self,
        file: SecretFile,
        http: httpx.Client,
        *,
        open_browser: Callable[[str], object],
        say: Callable[[str], object],
    ) -> None:
        self._file = file
        self._http = http
        self._open = open_browser
        self._say = say

    def run(self) -> None:
        values = self._file.values()
        missing = [
            name.value
            for name in (
                CredentialName.DRIVE_OAUTH_CLIENT_ID,
                CredentialName.DRIVE_OAUTH_CLIENT_SECRET,
            )
            if name not in values
        ]
        if missing:
            raise ConfigurationError(
                f"set {' and '.join(missing)} first with: enikk-ops secret set <name>"
            )
        client_id = values[CredentialName.DRIVE_OAUTH_CLIENT_ID]
        client_secret = values[CredentialName.DRIVE_OAUTH_CLIENT_SECRET]
        verifier, challenge = pkce_pair()
        state = secrets.token_urlsafe(16)
        receiver = LoopbackReceiver()
        try:
            url = authorization_url(
                client_id.get_secret_value(), receiver.redirect_uri, challenge, state
            )
            thread = receiver.start()
            self._say("Opening the browser to allow Drive access. If it does not open, visit:")
            self._say(url)
            self._open(url)
            thread.join(WAIT_SECONDS)
            params = dict(receiver.params)
        finally:
            receiver.close()
        if params.get("state") != state:
            raise DriveAuthError("no valid redirect from Google (timed out or wrong state)")
        if "code" not in params:
            raise DriveAuthError(f"access was not granted: {params.get('error', 'unknown')}")
        token = exchange_code(
            self._http,
            code=params["code"],
            client_id=client_id,
            client_secret=client_secret,
            redirect_uri=receiver.redirect_uri,
            verifier=verifier,
        )
        self._file.set(CredentialName.DRIVE_OAUTH_REFRESH_TOKEN, token)
