"""Immutable value objects (P4). Invalid instances cannot be constructed."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from enikk_core.errors import ValidationError

# RFC 1123 label: what Kubernetes requires for namespaces, release and resource names.
_DNS_LABEL = re.compile(r"^[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?$")


def _require_dns_label(value: str, what: str) -> None:
    if not _DNS_LABEL.match(value):
        raise ValidationError(f"{what} must be a lowercase RFC 1123 label, got {value!r}")


@dataclass(frozen=True, slots=True)
class ImageRef:
    repository: str
    tag: str = "latest"

    def __post_init__(self) -> None:
        if not self.repository or any(c.isspace() for c in self.repository):
            raise ValidationError(f"invalid image repository: {self.repository!r}")
        if not self.tag or ":" in self.tag or "/" in self.tag:
            raise ValidationError(f"invalid image tag: {self.tag!r}")

    def __str__(self) -> str:
        return f"{self.repository}:{self.tag}"


@dataclass(frozen=True, slots=True)
class KubeTarget:
    """Where to act: kube context + namespace."""

    context: str
    namespace: str

    def __post_init__(self) -> None:
        if not self.context:
            raise ValidationError("kube context must not be empty")
        _require_dns_label(self.namespace, "namespace")


@dataclass(frozen=True, slots=True)
class HelmRelease:
    name: str
    chart: Path
    target: KubeTarget
    values_files: tuple[Path, ...] = ()

    def __post_init__(self) -> None:
        _require_dns_label(self.name, "release name")


@dataclass(frozen=True, slots=True)
class ReleaseInfo:
    name: str
    revision: int
    status: str
