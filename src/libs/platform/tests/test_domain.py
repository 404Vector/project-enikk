from __future__ import annotations

from pathlib import Path

import pytest

from enikk_core.errors import ValidationError
from enikk_platform.domain import HelmRelease, ImageRef, KubeTarget


def test_image_ref_str() -> None:
    assert str(ImageRef("enikk/claude-agent", "0.1.0")) == "enikk/claude-agent:0.1.0"


@pytest.mark.parametrize("tag", ["", "a:b", "x/y"])
def test_image_ref_rejects_bad_tag(tag: str) -> None:
    with pytest.raises(ValidationError):
        ImageRef("repo", tag)


@pytest.mark.parametrize("ns", ["Enikk", "-bad", "a_b", ""])
def test_kube_target_rejects_invalid_namespace(ns: str) -> None:
    with pytest.raises(ValidationError):
        KubeTarget("docker-desktop", ns)


def test_release_name_validated() -> None:
    target = KubeTarget("docker-desktop", "enikk")
    with pytest.raises(ValidationError):
        HelmRelease("Bad_Name", Path("chart"), target)
