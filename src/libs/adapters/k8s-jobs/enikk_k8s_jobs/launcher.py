"""``JobLauncher`` on the Kubernetes API (ADR-0021).

The Job manifest comes from a ConfigMap rendered by the ``dev-agent`` Helm chart, so
image, ServiceAccount, security settings and limits are reviewed in git and deployed
by GitOps. Core only substitutes the task ID; it never builds a Pod spec itself.
"""

from __future__ import annotations

import logging
import ssl
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Final, Literal

import httpx
import pydantic_core
import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from enikk_core_domain.errors import DependencyUnavailableError
from enikk_kernel.models import FrozenModel

if TYPE_CHECKING:
    from collections.abc import Generator

_log = logging.getLogger(__name__)

SERVICE_ACCOUNT_DIR: Final = Path("/var/run/secrets/kubernetes.io/serviceaccount")
TASK_PLACEHOLDER: Final = "${TASK_ID}"
TEMPLATE_KEY: Final = "job.yaml"
ROLE_LABEL: Final = "enikk.dev/role"
ROLE_VALUE: Final = "dev-agent"
TASK_LABEL: Final = "enikk.dev/task"


class KubeJobConfig(FrozenModel):
    namespace: Annotated[str, Field(pattern=r"^[a-z0-9-]{1,63}$")]
    template_configmap: Annotated[str, Field(pattern=r"^[a-z0-9.-]{1,253}$")]
    api_url: str = "https://kubernetes.default.svc"
    ca_file: Path = SERVICE_ACCOUNT_DIR / "ca.crt"
    token_file: Path = SERVICE_ACCOUNT_DIR / "token"
    timeout_seconds: Annotated[float, Field(gt=0, le=30)] = 10.0


class FileBearerAuth(httpx.Auth):
    """Sends the token stored in ``path``, re-read on every request (kubelet rotates it)."""

    def __init__(self, path: Path) -> None:
        self._path = path

    def auth_flow(self, request: httpx.Request) -> Generator[httpx.Request, httpx.Response, None]:
        request.headers["Authorization"] = f"Bearer {self._path.read_text().strip()}"
        yield request


class _KubeBody(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")


class _ConfigMap(_KubeBody):
    data: dict[str, str] = {}


class _Metadata(_KubeBody):
    name: str
    namespace: str | None = None
    labels: dict[str, str] = {}


class _JobCondition(_KubeBody):
    type: str
    status: str


class _JobStatus(_KubeBody):
    conditions: tuple[_JobCondition, ...] = ()


class _Job(_KubeBody):
    kind: Literal["Job"]
    metadata: _Metadata
    status: _JobStatus = _JobStatus()

    @property
    def finished(self) -> bool:
        return any(
            c.type in {"Complete", "Failed"} and c.status == "True" for c in self.status.conditions
        )


class _JobList(_KubeBody):
    items: tuple[_Job, ...] = ()


class KubeJobLauncher:
    def __init__(self, http: httpx.AsyncClient, config: KubeJobConfig) -> None:
        self._http = http
        self._config = config

    @classmethod
    def in_cluster(cls, config: KubeJobConfig) -> KubeJobLauncher:
        http = httpx.AsyncClient(
            base_url=config.api_url,
            verify=ssl.create_default_context(cafile=config.ca_file),
            auth=FileBearerAuth(config.token_file),
            timeout=config.timeout_seconds,
        )
        return cls(http, config)

    async def aclose(self) -> None:
        await self._http.aclose()

    @property
    def _jobs_path(self) -> str:
        return f"/apis/batch/v1/namespaces/{self._config.namespace}/jobs"

    async def ready(self) -> bool:
        return await self._template() is not None

    async def running(self) -> int:
        response = await self._request(
            "GET", self._jobs_path, params={"labelSelector": f"{ROLE_LABEL}={ROLE_VALUE}"}
        )
        jobs = _parse(_JobList, response.content)
        return sum(1 for job in jobs.items if not job.finished)

    async def launch(self, task_id: uuid.UUID) -> str:
        template = await self._template()
        if template is None:
            raise DependencyUnavailableError("the dev agent Job template is not deployed")
        manifest = template.replace(TASK_PLACEHOLDER, str(uuid.UUID(str(task_id))))
        try:
            document = yaml.safe_load(manifest)
            job = _Job.model_validate_json(pydantic_core.to_json(document))
        except (yaml.YAMLError, ValidationError) as exc:
            raise DependencyUnavailableError(f"invalid dev agent Job template: {exc}") from exc
        self._check(job, task_id)
        response = await self._request(
            "POST",
            self._jobs_path,
            content=pydantic_core.to_json(document),
            headers={"Content-Type": "application/json"},
        )
        return _parse(_Job, response.content).metadata.name

    def _check(self, job: _Job, task_id: uuid.UUID) -> None:
        namespace = job.metadata.namespace
        if namespace is not None and namespace != self._config.namespace:
            raise DependencyUnavailableError(f"Job template targets namespace {namespace}")
        labels = job.metadata.labels
        if labels.get(ROLE_LABEL) != ROLE_VALUE or labels.get(TASK_LABEL) != str(task_id):
            raise DependencyUnavailableError(
                f"Job template must label {ROLE_LABEL}={ROLE_VALUE} and {TASK_LABEL}=<task>"
            )

    async def _template(self) -> str | None:
        path = (
            f"/api/v1/namespaces/{self._config.namespace}"
            f"/configmaps/{self._config.template_configmap}"
        )
        try:
            response = await self._request("GET", path)
        except _NotFoundError:
            return None
        return _parse(_ConfigMap, response.content).data.get(TEMPLATE_KEY)

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str] | None = None,
        content: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        try:
            response = await self._http.request(
                method, path, params=params, content=content, headers=headers
            )
        except httpx.HTTPError as exc:
            raise DependencyUnavailableError(f"Kubernetes {method} {path} failed: {exc}") from exc
        if response.status_code == 404:
            raise _NotFoundError(path)
        if not response.is_success:
            raise DependencyUnavailableError(
                f"Kubernetes {method} {path} returned {response.status_code}"
            )
        return response


class _NotFoundError(DependencyUnavailableError):
    pass


def _parse[T: BaseModel](model: type[T], content: bytes) -> T:
    try:
        return model.model_validate_json(content)
    except ValidationError as exc:
        raise DependencyUnavailableError(f"unexpected {model.__name__} from Kubernetes") from exc
