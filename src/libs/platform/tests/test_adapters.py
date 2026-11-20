from __future__ import annotations

import json
from pathlib import Path

from enikk_core.testing import RecordingRunner
from enikk_platform.adapters import DockerCli, HelmCli, KubectlCli
from enikk_platform.domain import HelmRelease, ImageRef, KubeTarget, ReleaseInfo

TARGET = KubeTarget("docker-desktop", "enikk")
RELEASE = HelmRelease("enikk-agent", Path("deploy/helm/enikk-agent"), TARGET)


def test_docker_build_argv() -> None:
    runner = RecordingRunner()
    DockerCli(runner).build(ImageRef("r", "t"), Path("ctx"), build_args={"A": "1"})
    assert runner.argvs == [("docker", "build", "-t", "r:t", "--build-arg", "A=1", "ctx")]
    assert runner.calls[0].stream


def test_helm_upgrade_uses_values_file_and_scope() -> None:
    runner = RecordingRunner()
    HelmCli(runner).upgrade_install(RELEASE, {"image": {"tag": "x"}}, timeout="2m")
    argv = runner.argvs[0]
    assert argv[:5] == ("helm", "upgrade", "--install", "enikk-agent", "deploy/helm/enikk-agent")
    assert ("--kube-context", "docker-desktop") == argv[argv.index("--kube-context") :][:2]
    assert "--values" in argv
    assert argv[-3:] == ("--wait", "--timeout", "2m")
    # temporary values file is cleaned up afterwards
    values_path = Path(argv[argv.index("--values") + 1])
    assert not values_path.exists()


def test_helm_status_parses_json() -> None:
    runner = RecordingRunner()
    payload = {"name": "enikk-agent", "version": 4, "info": {"status": "deployed"}}
    runner.when("helm", "status", stdout=json.dumps(payload))
    assert HelmCli(runner).status(RELEASE) == ReleaseInfo("enikk-agent", 4, "deployed")


def test_helm_status_missing_release() -> None:
    runner = RecordingRunner()
    runner.when("helm", "status", returncode=1, stderr="release: not found")
    assert HelmCli(runner).status(RELEASE) is None


def test_kubectl_apply_sends_manifest_on_stdin() -> None:
    runner = RecordingRunner()
    KubectlCli(runner).apply(TARGET, {"kind": "Secret", "stringData": {"K": "sk-secret"}})
    call = runner.calls[0]
    assert "sk-secret" not in " ".join(call.args)
    assert call.input is not None
    assert json.loads(call.input)["stringData"]["K"] == "sk-secret"


def test_kubectl_exists() -> None:
    runner = RecordingRunner()
    runner.when("kubectl", "--context", "docker-desktop", "--namespace", "enikk", "get", returncode=1)
    assert KubectlCli(runner).exists(TARGET, "secret", "x") is False
