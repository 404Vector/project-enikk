from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest

from enikk_core_domain.errors import DependencyUnavailableError
from enikk_k8s_jobs.applications import ArgoConfig, KubeApplicationReader

pytestmark = pytest.mark.anyio

SHA = "d" * 40
APPLICATION = {
    "apiVersion": "argoproj.io/v1alpha1",
    "kind": "Application",
    "metadata": {"name": "enikk-core", "namespace": "argocd"},
    "spec": {
        "source": {
            "helm": {
                "parameters": [
                    {"name": "image.repository", "value": "ghcr.io/404vector/enikk-core"},
                    {"name": "image.tag", "value": f"sha-{SHA}", "forceString": True},
                ]
            }
        }
    },
    "status": {
        "health": {"status": "Healthy"},
        "operationState": {
            "phase": "Succeeded",
            "message": "successfully synced (no more tasks)",
            "startedAt": "2026-11-22T09:00:50Z",
            "syncResult": {"revision": SHA},
        },
    },
}


def _reader(handler: httpx.MockTransport) -> KubeApplicationReader:
    http = httpx.AsyncClient(base_url="https://kube.test", transport=handler)
    return KubeApplicationReader(http, ArgoConfig())


async def test_reads_image_tag_operation_and_health() -> None:
    paths: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if request.url.path.endswith("/enikk-core"):
            return httpx.Response(200, json=APPLICATION)
        return httpx.Response(404, json={"kind": "Status"})

    [observation] = await _reader(httpx.MockTransport(handle)).observe(["enikk-core", "missing"])
    assert paths == [
        "/apis/argoproj.io/v1alpha1/namespaces/argocd/applications/enikk-core",
        "/apis/argoproj.io/v1alpha1/namespaces/argocd/applications/missing",
    ]
    assert observation.app == "enikk-core"
    assert observation.image_tag == f"sha-{SHA}"
    assert observation.operation_started_at == datetime(2026, 11, 22, 9, 0, 50, tzinfo=UTC)
    assert observation.operation_phase == "Succeeded"
    assert observation.operation_revision == SHA
    assert observation.health == "Healthy"


async def test_application_without_an_operation_yet() -> None:
    bare = {"kind": "Application", "metadata": {"name": "enikk-dev-agent"}, "status": {}}
    reader = _reader(httpx.MockTransport(lambda _: httpx.Response(200, json=bare)))
    [observation] = await reader.observe(["enikk-dev-agent"])
    assert observation.image_tag is None
    assert observation.operation_started_at is None
    assert observation.health == "Unknown"


async def test_forbidden_is_a_dependency_error() -> None:
    reader = _reader(httpx.MockTransport(lambda _: httpx.Response(403, json={})))
    with pytest.raises(DependencyUnavailableError, match="403"):
        await reader.observe(["enikk-core"])
