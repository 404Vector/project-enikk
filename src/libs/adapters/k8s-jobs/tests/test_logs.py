from __future__ import annotations

import httpx
import pytest

from enikk_k8s_jobs.logs import MAX_LINES, KubeLogReader

pytestmark = pytest.mark.anyio


async def test_lists_pods_by_label_and_tails_their_logs() -> None:
    seen: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path.endswith("/pods"):
            return httpx.Response(200, json={"items": [{"metadata": {"name": "core-1"}}]})
        return httpx.Response(200, text="one\ntwo\n")

    reader = KubeLogReader(
        httpx.AsyncClient(base_url="https://kube.test", transport=httpx.MockTransport(handle))
    )
    assert await reader.pods("enikk", "app.kubernetes.io/name=enikk-core") == ["core-1"]
    assert await reader.tail("enikk", "core-1", 600) == ["one", "two"]
    assert seen[0].url.params["labelSelector"] == "app.kubernetes.io/name=enikk-core"
    assert seen[1].url.path == "/api/v1/namespaces/enikk/pods/core-1/log"
    assert seen[1].url.params["tailLines"] == str(MAX_LINES)
    assert seen[1].url.params["sinceSeconds"] == "600"
