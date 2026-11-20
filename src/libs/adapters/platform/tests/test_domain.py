from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from enikk_platform.domain import HelmRelease, ImageRef, KubeTarget


def test_image_ref_str() -> None:
    assert str(ImageRef(repository="ghcr.io/x/y", tag="abc123")) == "ghcr.io/x/y:abc123"


@pytest.mark.parametrize("tag", ["", "a:b", "x/y", "-lead"])
def test_image_ref_rejects_bad_tag(tag: str) -> None:
    with pytest.raises(ValidationError):
        ImageRef(repository="repo", tag=tag)


@pytest.mark.parametrize("namespace", ["Enikk", "-bad", "a_b", ""])
def test_kube_target_rejects_invalid_namespace(namespace: str) -> None:
    with pytest.raises(ValidationError):
        KubeTarget(context="docker-desktop", namespace=namespace)


def test_release_name_validated() -> None:
    target = KubeTarget(context="docker-desktop", namespace="enikk")
    with pytest.raises(ValidationError):
        HelmRelease(name="Bad_Name", chart=Path("chart"), target=target)
