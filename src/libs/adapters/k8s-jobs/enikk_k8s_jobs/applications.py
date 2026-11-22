"""``ApplicationReader`` on the Kubernetes API: reads Argo CD Applications (ADR-0023).

Core may only ``get`` the named Applications in the Argo CD namespace (I-28).
"""

from __future__ import annotations

import ssl
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Final

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from enikk_core_domain.deployments import AppObservation
from enikk_core_domain.errors import DependencyUnavailableError
from enikk_k8s_jobs.launcher import SERVICE_ACCOUNT_DIR, FileBearerAuth
from enikk_kernel.models import FrozenModel

if TYPE_CHECKING:
    from collections.abc import Sequence

IMAGE_TAG_PARAMETER: Final = "image.tag"


class ArgoConfig(FrozenModel):
    namespace: Annotated[str, Field(pattern=r"^[a-z0-9-]{1,63}$")] = "argocd"
    api_url: str = "https://kubernetes.default.svc"
    ca_file: Path = SERVICE_ACCOUNT_DIR / "ca.crt"
    token_file: Path = SERVICE_ACCOUNT_DIR / "token"
    timeout_seconds: Annotated[float, Field(gt=0, le=30)] = 10.0


class _Body(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore", populate_by_name=True)


class _Parameter(_Body):
    name: str
    value: str = ""


class _Helm(_Body):
    parameters: tuple[_Parameter, ...] = ()


class _Source(_Body):
    helm: _Helm = _Helm()


class _Spec(_Body):
    source: _Source = _Source()


class _SyncResult(_Body):
    revision: str | None = None


class _Operation(_Body):
    phase: str | None = None
    message: str = ""
    started_at: datetime | None = Field(default=None, alias="startedAt")
    sync_result: _SyncResult = Field(default=_SyncResult(), alias="syncResult")


class _Health(_Body):
    status: str = "Unknown"


class _Status(_Body):
    operation_state: _Operation | None = Field(default=None, alias="operationState")
    health: _Health = _Health()


class _Application(_Body):
    spec: _Spec = _Spec()
    status: _Status = _Status()


class KubeApplicationReader:
    def __init__(self, http: httpx.AsyncClient, config: ArgoConfig) -> None:
        self._http = http
        self._config = config

    @classmethod
    def in_cluster(cls, config: ArgoConfig) -> KubeApplicationReader:
        http = httpx.AsyncClient(
            base_url=config.api_url,
            verify=ssl.create_default_context(cafile=config.ca_file),
            auth=FileBearerAuth(config.token_file),
            timeout=config.timeout_seconds,
        )
        return cls(http, config)

    async def aclose(self) -> None:
        await self._http.aclose()

    async def observe(self, apps: Sequence[str]) -> list[AppObservation]:
        observations = []
        for name in apps:
            app = await self._get(name)
            if app is not None:
                observations.append(_observation(name, app))
        return observations

    async def _get(self, name: str) -> _Application | None:
        path = f"/apis/argoproj.io/v1alpha1/namespaces/{self._config.namespace}/applications/{name}"
        try:
            response = await self._http.get(path)
        except httpx.HTTPError as exc:
            raise DependencyUnavailableError(f"Kubernetes GET {path} failed: {exc}") from exc
        if response.status_code == 404:
            return None
        if not response.is_success:
            raise DependencyUnavailableError(
                f"Kubernetes GET {path} returned {response.status_code}"
            )
        try:
            return _Application.model_validate_json(response.content)
        except ValidationError as exc:
            raise DependencyUnavailableError(f"unexpected Argo CD Application {name}") from exc


def _observation(name: str, app: _Application) -> AppObservation:
    tags = [p.value for p in app.spec.source.helm.parameters if p.name == IMAGE_TAG_PARAMETER]
    operation = app.status.operation_state
    return AppObservation(
        app=name,
        image_tag=tags[0] if tags and tags[0] else None,
        operation_started_at=operation.started_at if operation else None,
        operation_phase=operation.phase if operation else None,
        operation_revision=operation.sync_result.revision if operation else None,
        operation_message=(operation.message if operation else "")[:500],
        health=app.status.health.status,
    )
