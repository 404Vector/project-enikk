from __future__ import annotations

import uuid

import httpx
import pytest
from pydantic import BaseModel

from enikk_core_domain.errors import DependencyUnavailableError
from enikk_k8s_jobs.launcher import KubeJobConfig, KubeJobLauncher

pytestmark = pytest.mark.anyio

CONFIG = KubeJobConfig(namespace="enikk-dev", template_configmap="enikk-dev-agent-job")
TASK = uuid.UUID("12345678-1234-5678-1234-567812345678")
CONFIGMAP_PATH = "/api/v1/namespaces/enikk-dev/configmaps/enikk-dev-agent-job"
JOBS_PATH = "/apis/batch/v1/namespaces/enikk-dev/jobs"

TEMPLATE = """\
apiVersion: batch/v1
kind: Job
metadata:
  name: enikk-dev-${TASK_ID}
  namespace: enikk-dev
  labels:
    enikk.dev/role: dev-agent
    enikk.dev/task: "${TASK_ID}"
spec:
  template:
    spec:
      containers:
        - name: dev-agent
          env:
            - name: ENIKK_DEV_TASK_ID
              value: "${TASK_ID}"
"""


class _Sent(BaseModel):
    kind: str
    metadata: dict[str, object]
    spec: dict[str, object]


def _launcher(
    template: str | None, jobs: list[dict[str, object]] | None = None
) -> tuple[KubeJobLauncher, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == CONFIGMAP_PATH:
            if template is None:
                return httpx.Response(404, json={"kind": "Status"})
            return httpx.Response(200, json={"data": {"job.yaml": template}})
        if request.url.path == JOBS_PATH and request.method == "GET":
            return httpx.Response(200, json={"items": jobs or []})
        if request.url.path == JOBS_PATH and request.method == "POST":
            return httpx.Response(201, content=request.content)
        return httpx.Response(500)

    http = httpx.AsyncClient(base_url="https://k8s.test", transport=httpx.MockTransport(handler))
    return KubeJobLauncher(http, CONFIG), seen


async def test_launch_fills_only_the_task_id() -> None:
    launcher, seen = _launcher(TEMPLATE)
    name = await launcher.launch(TASK)
    assert name == f"enikk-dev-{TASK}"
    posted = _Sent.model_validate_json(seen[-1].content)
    assert posted.kind == "Job"
    assert "${TASK_ID}" not in seen[-1].content.decode()
    assert str(TASK) in seen[-1].content.decode()


async def test_missing_template_means_not_ready() -> None:
    launcher, _ = _launcher(None)
    assert not await launcher.ready()
    with pytest.raises(DependencyUnavailableError, match="not deployed"):
        await launcher.launch(TASK)


async def test_template_must_stay_in_the_namespace_and_carry_labels() -> None:
    elsewhere, _ = _launcher(TEMPLATE.replace("namespace: enikk-dev", "namespace: enikk"))
    with pytest.raises(DependencyUnavailableError, match="namespace enikk"):
        await elsewhere.launch(TASK)
    unlabeled, seen = _launcher(TEMPLATE.replace("enikk.dev/role: dev-agent", "x: y"))
    with pytest.raises(DependencyUnavailableError, match="must label"):
        await unlabeled.launch(TASK)
    assert all(r.method == "GET" for r in seen)


async def test_running_counts_unfinished_jobs() -> None:
    jobs: list[dict[str, object]] = [
        {"kind": "Job", "metadata": {"name": "a"}, "status": {}},
        {
            "kind": "Job",
            "metadata": {"name": "b"},
            "status": {"conditions": [{"type": "Complete", "status": "True"}]},
        },
        {
            "kind": "Job",
            "metadata": {"name": "c"},
            "status": {"conditions": [{"type": "Failed", "status": "True"}]},
        },
    ]
    launcher, seen = _launcher(TEMPLATE, jobs)
    assert await launcher.running() == 1
    assert seen[0].url.params["labelSelector"] == "enikk.dev/role=dev-agent"


async def test_api_errors_become_dependency_errors() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"kind": "Status"})

    http = httpx.AsyncClient(base_url="https://k8s.test", transport=httpx.MockTransport(handler))
    with pytest.raises(DependencyUnavailableError, match="403"):
        await KubeJobLauncher(http, CONFIG).running()
