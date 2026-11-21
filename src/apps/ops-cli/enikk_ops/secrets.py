"""Credentials kept on this PC and synced to core's Secret (ADR-0013)."""

from __future__ import annotations

import os
import stat
from typing import TYPE_CHECKING, Final, Literal

import pydantic_core
import yaml
from pydantic import AwareDatetime, Field

from enikk_contracts.credentials import CredentialName, CredentialValue
from enikk_kernel.errors import ConfigurationError
from enikk_kernel.models import FrozenModel
from enikk_platform.domain import KubeTarget

if TYPE_CHECKING:
    from pathlib import Path

    from pydantic import SecretStr

    from enikk_kernel.clock import Clock
    from enikk_ops.settings import SecretsSettings
    from enikk_platform.ports import ClusterClient

FILE_MODE: Final = 0o600
DIR_MODE: Final = 0o700
FIELD_MANAGER: Final = "enikk-ops"
HEADER: Final = "# enikk credentials (ADR-0013). Plain text: keep this file private.\n"


class StoredSecret(FrozenModel):
    value: CredentialValue
    updated_at: AwareDatetime


class SecretDocument(FrozenModel):
    version: Literal[1] = 1
    secrets: dict[CredentialName, StoredSecret] = Field(default_factory=dict)


class SecretStatus(FrozenModel):
    name: CredentialName
    updated_at: AwareDatetime | None


class SecretFile:
    """Plain-text YAML readable by the owner only. Every change rewrites it atomically."""

    def __init__(self, path: Path, clock: Clock) -> None:
        self.path = path
        self._clock = clock

    def load(self) -> SecretDocument:
        if not self.path.exists():
            return SecretDocument()
        mode = stat.S_IMODE(self.path.stat().st_mode)
        if mode & 0o077:
            raise ConfigurationError(
                f"{self.path} is readable by other users (mode {mode:o}); "
                f"run: chmod 600 {self.path}"
            )
        raw = yaml.safe_load(self.path.read_text(encoding="utf-8")) or {}
        return SecretDocument.model_validate_json(pydantic_core.to_json(raw))

    def set(self, name: CredentialName, value: SecretStr) -> None:
        document = self.load()
        stored = StoredSecret(value=value, updated_at=self._clock.now())
        self._save(SecretDocument(secrets={**document.secrets, name: stored}))

    def unset(self, name: CredentialName) -> bool:
        document = self.load()
        if name not in document.secrets:
            return False
        remaining = {key: value for key, value in document.secrets.items() if key != name}
        self._save(SecretDocument(secrets=remaining))
        return True

    def statuses(self) -> list[SecretStatus]:
        secrets = self.load().secrets
        return [
            SecretStatus(
                name=name, updated_at=secrets[name].updated_at if name in secrets else None
            )
            for name in CredentialName
        ]

    def values(self) -> dict[CredentialName, SecretStr]:
        return {name: stored.value for name, stored in self.load().secrets.items()}

    def _save(self, document: SecretDocument) -> None:
        self.path.parent.mkdir(mode=DIR_MODE, parents=True, exist_ok=True)
        body = HEADER + yaml.safe_dump(document.model_dump(mode="json"), sort_keys=True)
        temporary = self.path.with_name(f".{self.path.name}.tmp")
        temporary.unlink(missing_ok=True)
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, FILE_MODE)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(body)
        temporary.replace(self.path)


class CredentialSync:
    """Copies every credential in the file into core's Secret, removing the rest."""

    def __init__(
        self, settings: SecretsSettings, context: str, file: SecretFile, cluster: ClusterClient
    ) -> None:
        self._settings = settings
        self._target = KubeTarget(context=context, namespace=settings.namespace)
        self._file = file
        self._cluster = cluster

    def run(self) -> list[CredentialName]:
        values = self._file.values()
        if not values:
            raise ConfigurationError(
                f"no credentials in {self._file.path}; add one with: enikk-ops secret set <name>"
            )
        self._cluster.apply_secret(
            self._target,
            self._settings.secret_name,
            {name.value: value for name, value in values.items()},
            manager=FIELD_MANAGER,
        )
        return sorted(values)
