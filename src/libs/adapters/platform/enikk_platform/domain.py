"""Value objects. Invalid instances cannot be constructed."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Annotated

from pydantic import AfterValidator, Field

from enikk_kernel.models import FrozenModel

# RFC 1123 label: required by Kubernetes for namespaces, release and resource names.
_DNS_LABEL = re.compile(r"^[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?$")


def _check_dns_label(value: str) -> str:
    if not _DNS_LABEL.match(value):
        raise ValueError(f"must be a lowercase RFC 1123 label, got {value!r}")
    return value


DnsLabel = Annotated[str, AfterValidator(_check_dns_label)]


class ImageRef(FrozenModel):
    repository: str = Field(min_length=1, pattern=r"^\S+$")
    tag: str = Field(default="latest", pattern=r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$")

    def __str__(self) -> str:
        return f"{self.repository}:{self.tag}"


class KubeTarget(FrozenModel):
    """Where to act: kube context and namespace."""

    context: str = Field(min_length=1)
    namespace: DnsLabel


class HelmRelease(FrozenModel):
    name: DnsLabel
    chart: Path
    target: KubeTarget
    values_files: tuple[Path, ...] = ()


class ReleaseInfo(FrozenModel):
    name: str
    revision: int
    status: str
