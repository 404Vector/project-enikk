from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from enikk_kernel.errors import ConfigurationError
from enikk_kernel.testing import RecordingRunner
from enikk_ops.bootstrap import ArgoCdService
from enikk_ops.cli import app
from enikk_ops.settings import ArgoCdSettings, OpsSettings
from enikk_platform.adapters import HelmCli, KubectlCli


def _settings(root: Path) -> OpsSettings:
    values = root / "values.yaml"
    root_app = root / "root.yaml"
    values.write_text("{}\n")
    root_app.write_text("kind: Application\n")
    return OpsSettings(
        repo_root=root,
        argocd=ArgoCdSettings(values_file=Path("values.yaml"), root_application=Path("root.yaml")),
    )


def test_bootstrap_installs_pinned_chart_then_applies_root_application(tmp_path: Path) -> None:
    runner = RecordingRunner()
    service = ArgoCdService(_settings(tmp_path), HelmCli(runner), KubectlCli(runner))
    service.bootstrap()

    helm, kubectl = runner.argvs
    assert helm[:4] == ("helm", "upgrade", "--install", "argocd")
    assert helm[helm.index("--version") + 1] == "10.18.2"
    assert str(tmp_path / "values.yaml") in helm
    assert kubectl[0] == "kubectl"
    assert kubectl[-3:] == ("apply", "-f", str(tmp_path / "root.yaml"))


def test_bootstrap_requires_files_in_repository(tmp_path: Path) -> None:
    runner = RecordingRunner()
    settings = OpsSettings(repo_root=tmp_path)
    with pytest.raises(ConfigurationError):
        ArgoCdService(settings, HelmCli(runner), KubectlCli(runner)).bootstrap()
    assert runner.calls == []


def test_cli_help_lists_commands() -> None:
    result = CliRunner().invoke(app, ["argocd", "--help"])
    assert result.exit_code == 0
    for command in ("bootstrap", "status", "ui"):
        assert command in result.output


def test_ui_tells_how_to_retire_the_initial_admin_secret() -> None:
    from enikk_ops.cli import initial_password_steps

    steps = initial_password_steps("argocd")
    assert "get secret argocd-initial-admin-secret" in steps
    assert "kubectl -n argocd delete secret argocd-initial-admin-secret" in steps
