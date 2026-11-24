"""The app list the images workflow reads (ADR-0040)."""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[4]
APP_NAME = re.compile(r"^[a-z][a-z0-9-]{0,40}$")


def _apps() -> list[str]:
    document = yaml.safe_load((REPO / "deploy" / "images.yaml").read_text())
    apps = document["apps"]
    assert isinstance(apps, list)
    return [str(app) for app in apps]


def test_names_are_plain_and_unique() -> None:
    apps = _apps()
    assert apps
    assert len(apps) == len(set(apps))
    assert all(APP_NAME.fullmatch(app) for app in apps)


def test_each_app_is_a_package_named_after_it() -> None:
    for app in _apps():
        project = tomllib.loads((REPO / "src" / "apps" / app / "pyproject.toml").read_text())
        assert project["project"]["name"] == f"enikk-{app}"


def test_every_chart_has_an_image() -> None:
    charts = {path.name for path in (REPO / "deploy" / "helm").iterdir() if path.is_dir()}
    assert charts <= set(_apps())
