"""Read-only Kubernetes credentials in the run's home (ADR-0041 3c)."""

from __future__ import annotations

import stat
from typing import TYPE_CHECKING

from enikk_engine_claude.kube_access import (
    CA_FILE,
    KUBECONFIG,
    SERVER_FILE,
    TOKEN_FILE,
    KubeAccess,
    install,
    main,
)

if TYPE_CHECKING:
    from pathlib import Path

    import pytest


def _mounted(tmp_path: Path) -> KubeAccess:
    mount = tmp_path / "mount"
    mount.mkdir()
    (mount / "token").write_text("secret-value\n")
    (mount / "ca.crt").write_text("CA\n")
    return KubeAccess(token_file=mount / "token", ca_file=mount / "ca.crt")


def test_install_copies_the_token_ca_and_a_kubeconfig(tmp_path: Path) -> None:
    home = tmp_path / "home"
    assert install(home, _mounted(tmp_path))
    assert (home / TOKEN_FILE).read_text() == "secret-value\n"
    assert (home / CA_FILE).read_text() == "CA\n"
    assert (home / SERVER_FILE).read_text() == "https://kubernetes.default.svc\n"
    config = (home / KUBECONFIG).read_text()
    assert f"tokenFile: {home / TOKEN_FILE}" in config
    assert "secret-value" not in config
    for path in (TOKEN_FILE, CA_FILE, KUBECONFIG):
        assert stat.S_IMODE((home / path).stat().st_mode) == 0o600


def test_nothing_is_installed_without_the_mount(tmp_path: Path) -> None:
    access = KubeAccess(token_file=tmp_path / "none", ca_file=tmp_path / "none.crt")
    assert not install(tmp_path / "home", access)
    assert not (tmp_path / "home").exists()


def test_the_helper_needs_the_token(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    assert main(["/api/v1/namespaces/enikk/pods"]) == 2
