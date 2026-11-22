from __future__ import annotations

import base64
import stat
from datetime import timedelta
from typing import TYPE_CHECKING

import pytest
from pydantic import SecretStr
from typer.testing import CliRunner

from enikk_contracts.credentials import CredentialName
from enikk_kernel.errors import ConfigurationError
from enikk_kernel.models import FrozenModel
from enikk_kernel.testing import ManualClock, RecordingRunner
from enikk_ops.cli import app
from enikk_ops.secrets import CredentialSync, SecretFile
from enikk_ops.settings import SecretsSettings
from enikk_platform.adapters import KubectlCli

if TYPE_CHECKING:
    from pathlib import Path

BOT = CredentialName.SLACK_BOT_TOKEN
APP = CredentialName.SLACK_APP_TOKEN


class _Manifest(FrozenModel, extra="ignore"):
    data: dict[str, str]


def _file(tmp_path: Path, clock: ManualClock | None = None) -> SecretFile:
    return SecretFile(tmp_path / "cfg" / "enikk" / "secrets.yaml", clock or ManualClock())


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def _access(path: Path) -> int:
    """Read, write and execute bits only. A new directory inherits the setgid bit of a
    setgid parent (a Kubernetes fsGroup volume is one); that bit grants no access."""
    return _mode(path) & 0o777


def test_set_writes_private_plain_text_file(tmp_path: Path) -> None:
    file = _file(tmp_path)
    file.set(BOT, SecretStr("xoxb-1"))
    assert _mode(file.path) == 0o600
    assert _access(file.path.parent) == 0o700
    assert "xoxb-1" in file.path.read_text()
    assert file.values()[BOT].get_secret_value() == "xoxb-1"


def test_set_stays_private_under_a_setgid_group_writable_parent(tmp_path: Path) -> None:
    tmp_path.chmod(0o2777)
    file = _file(tmp_path)
    file.set(BOT, SecretStr("xoxb-1"))
    assert _mode(file.path) == 0o600
    assert _access(file.path.parent) == 0o700


def test_set_replaces_and_unset_removes(tmp_path: Path) -> None:
    clock = ManualClock()
    file = _file(tmp_path, clock)
    file.set(BOT, SecretStr("old"))
    clock.advance(timedelta(minutes=5))
    file.set(BOT, SecretStr("new"))
    file.set(APP, SecretStr("xapp-1"))
    assert file.values()[BOT].get_secret_value() == "new"
    assert file.unset(APP) is True
    assert file.unset(APP) is False
    statuses = {s.name: s.updated_at for s in file.statuses()}
    assert statuses[BOT] == clock.now()
    assert statuses[APP] is None
    assert set(statuses) == set(CredentialName)


def test_missing_file_is_empty(tmp_path: Path) -> None:
    assert _file(tmp_path).values() == {}


def test_file_readable_by_others_is_refused(tmp_path: Path) -> None:
    file = _file(tmp_path)
    file.set(BOT, SecretStr("xoxb-1"))
    file.path.chmod(0o644)
    with pytest.raises(ConfigurationError, match="chmod 600"):
        file.load()


def test_sync_applies_every_local_credential(tmp_path: Path) -> None:
    file = _file(tmp_path)
    file.set(BOT, SecretStr("xoxb-1"))
    file.set(APP, SecretStr("xapp-1"))
    runner = RecordingRunner()
    synced = CredentialSync(SecretsSettings(), "docker-desktop", file, KubectlCli(runner)).run()
    assert synced == [APP, BOT]
    call = runner.calls[0]
    assert "--server-side" in call.argv
    assert not any("xoxb" in arg for arg in call.argv)
    assert call.stdin is not None
    data = _Manifest.model_validate_json(call.stdin).data
    assert base64.b64decode(data["slack.bot-token"]) == b"xoxb-1"
    assert set(data) == {"slack.bot-token", "slack.app-token"}


def test_sync_refuses_an_empty_file(tmp_path: Path) -> None:
    runner = RecordingRunner()
    sync = CredentialSync(SecretsSettings(), "docker-desktop", _file(tmp_path), KubectlCli(runner))
    with pytest.raises(ConfigurationError, match="secret set"):
        sync.run()
    assert runner.calls == []


def test_cli_set_reads_hidden_prompt_and_list_hides_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    cli = CliRunner()
    stored = cli.invoke(app, ["secret", "set", "slack.bot-token"], input="xoxb-cli\n")
    assert stored.exit_code == 0, stored.output
    assert "xoxb-cli" not in stored.output
    listed = cli.invoke(app, ["secret", "list"])
    assert listed.exit_code == 0, listed.output
    assert "slack.bot-token" in listed.output
    assert "xoxb-cli" not in listed.output
    assert "xoxb-cli" in (tmp_path / ".config" / "enikk" / "secrets.yaml").read_text()


def test_cli_set_reads_a_multi_line_value_from_a_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    pem = "-----BEGIN RSA PRIVATE KEY-----\nline-one\nline-two\n-----END RSA PRIVATE KEY-----\n"
    source = tmp_path / "app.pem"
    source.write_text(pem)
    stored = CliRunner().invoke(
        app, ["secret", "set", "github.app-private-key", "--from-file", str(source)]
    )
    assert stored.exit_code == 0, stored.output
    assert "line-one" not in stored.output
    secrets = (tmp_path / ".config" / "enikk" / "secrets.yaml").read_text()
    assert "line-one" in secrets
    assert "line-two" in secrets


def test_cli_set_refuses_an_empty_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    source = tmp_path / "empty.pem"
    source.write_text("\n")
    stored = CliRunner().invoke(
        app, ["secret", "set", "github.app-private-key", "--from-file", str(source)]
    )
    assert stored.exit_code != 0
