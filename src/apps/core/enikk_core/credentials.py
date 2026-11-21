"""Credentials core hands to other services (ADR-0013)."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Final

from pydantic import SecretStr

from enikk_contracts.credentials import Credentials
from enikk_core_domain.errors import NotFoundError, PermissionDeniedError
from enikk_kernel.errors import ConfigurationError

if TYPE_CHECKING:
    from pathlib import Path

    from enikk_contracts.credentials import CredentialName, CredentialRequest
    from enikk_core_domain.auth import Principal
    from enikk_core_domain.ports import CredentialStore

MAX_VALUE_BYTES: Final = 16_384

_log = logging.getLogger(__name__)


class DirectoryCredentialStore:
    """Reads one file per credential from a mounted Secret volume.

    Kubernetes updates the files in place when the Secret changes, and every
    call reads them again, so a ``secret sync`` applies without a restart.
    """

    def __init__(self, directory: Path) -> None:
        self._directory = directory

    def get(self, name: CredentialName) -> SecretStr | None:
        try:
            raw = (self._directory / name.value).read_bytes()
        except FileNotFoundError:
            return None
        if len(raw) > MAX_VALUE_BYTES:
            raise ConfigurationError(f"credential {name} exceeds {MAX_VALUE_BYTES} bytes")
        value = raw.decode("utf-8").strip()
        return SecretStr(value) if value else None


class CredentialIssuer:
    def __init__(self, store: CredentialStore) -> None:
        self._store = store

    def issue(self, principal: Principal, request: CredentialRequest) -> Credentials:
        denied = sorted(name for name in request.names if name not in principal.credentials)
        if denied:
            raise PermissionDeniedError(f"{principal.identity} may not read {', '.join(denied)}")
        values: dict[CredentialName, SecretStr] = {}
        missing: list[str] = []
        for name in request.names:
            value = self._store.get(name)
            if value is None:
                missing.append(name)
            else:
                values[name] = value
        if missing:
            raise NotFoundError(f"credentials not set: {', '.join(sorted(missing))}")
        _log.info("credentials %s issued to %s", ", ".join(request.names), principal.identity)
        return Credentials(values=values)
