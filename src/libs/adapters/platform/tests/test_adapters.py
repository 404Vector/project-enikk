from __future__ import annotations

from pathlib import Path

from enikk_kernel.models import FrozenModel
from enikk_kernel.testing import RecordingRunner
from enikk_platform.adapters import DockerCli, HelmCli, KubectlCli
from enikk_platform.domain import (
    HelmRelease,
    ImageRef,
    KubeTarget,
    LocalChart,
    ReleaseInfo,
    RemoteChart,
)

TARGET = KubeTarget(context="docker-desktop", namespace="enikk")
RELEASE = HelmRelease(
    name="enikk-core", chart=LocalChart(path=Path("deploy/helm/core")), target=TARGET
)


class _Image(FrozenModel):
    tag: str


class _Values(FrozenModel):
    image: _Image


class _Secret(FrozenModel):
    kind: str
    token: str


def test_docker_build_argv() -> None:
    runner = RecordingRunner()
    DockerCli(runner).build(ImageRef(repository="r", tag="t"), Path("ctx"))
    assert runner.argvs == [("docker", "build", "-t", "r:t", "ctx")]
    assert runner.calls[0].stream


def test_helm_upgrade_uses_values_file_and_scope() -> None:
    runner = RecordingRunner()
    HelmCli(runner).upgrade_install(RELEASE, _Values(image=_Image(tag="x")), timeout="2m")
    argv = runner.argvs[0]
    assert argv[:5] == ("helm", "upgrade", "--install", "enikk-core", "deploy/helm/core")
    assert argv[argv.index("--kube-context") + 1] == "docker-desktop"
    assert argv[-3:] == ("--wait", "--timeout", "2m")
    values_path = Path(argv[argv.index("--values") + 1])
    assert not values_path.exists()  # temporary file is removed afterwards


def test_helm_remote_chart_is_pinned() -> None:
    runner = RecordingRunner()
    chart = RemoteChart(repository="https://charts.example.com/", name="argo-cd", version="1.2.3")
    release = HelmRelease(name="argocd", chart=chart, target=TARGET)
    HelmCli(runner).upgrade_install(release, _Values(image=_Image(tag="x")), wait=False)
    argv = runner.argvs[0]
    assert argv[3:9] == (
        "argocd",
        "argo-cd",
        "--repo",
        "https://charts.example.com/",
        "--version",
        "1.2.3",
    )


def test_kubectl_apply_file_and_port_forward() -> None:
    runner = RecordingRunner()
    kubectl = KubectlCli(runner)
    kubectl.apply_file(TARGET, Path("deploy/argocd/root.yaml"))
    kubectl.port_forward(TARGET, "svc/argocd-server", 8080, 80)
    assert runner.argvs[0][-3:] == ("apply", "-f", "deploy/argocd/root.yaml")
    assert runner.argvs[1][-3:] == ("port-forward", "svc/argocd-server", "8080:80")
    assert runner.calls[1].mutating is False


def test_helm_status_parses_output_and_ignores_unknown_fields() -> None:
    runner = RecordingRunner()
    output = (
        '{"name": "enikk-core", "version": 4,'
        ' "info": {"status": "deployed", "notes": ""}, "manifest": ""}'
    )
    runner.when("helm", "status", stdout=output)
    assert HelmCli(runner).status(RELEASE) == ReleaseInfo(
        name="enikk-core", revision=4, status="deployed"
    )


def test_helm_status_missing_release() -> None:
    runner = RecordingRunner()
    runner.when("helm", "status", returncode=1, stderr="release: not found")
    assert HelmCli(runner).status(RELEASE) is None


def test_kubectl_apply_sends_manifest_on_stdin_only() -> None:
    runner = RecordingRunner()
    KubectlCli(runner).apply(TARGET, _Secret(kind="Secret", token="sk-test-value"))
    call = runner.calls[0]
    assert "sk-test-value" not in " ".join(call.argv)
    assert call.stdin is not None
    assert _Secret.model_validate_json(call.stdin).token == "sk-test-value"


def test_kubectl_namespace_manifest_uses_kubernetes_field_names() -> None:
    runner = RecordingRunner()
    KubectlCli(runner).ensure_namespace(TARGET)
    stdin = runner.calls[0].stdin
    assert stdin is not None
    assert '"apiVersion":"v1"' in stdin
    assert '"kind":"Namespace"' in stdin


def test_kubectl_exists() -> None:
    runner = RecordingRunner()
    runner.when(
        "kubectl", "--context", "docker-desktop", "--namespace", "enikk", "get", returncode=1
    )
    assert KubectlCli(runner).exists(TARGET, "secret", "x") is False
