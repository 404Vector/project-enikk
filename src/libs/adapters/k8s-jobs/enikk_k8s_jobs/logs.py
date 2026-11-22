"""Reading recent Pod logs of enikk's own services (ADR-0029). Read-only."""

from __future__ import annotations

import ssl
from pathlib import Path
from typing import Annotated, Final

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from enikk_core_domain.errors import DependencyUnavailableError
from enikk_k8s_jobs.launcher import SERVICE_ACCOUNT_DIR, FileBearerAuth
from enikk_kernel.models import FrozenModel

MAX_LINES: Final = 200


class LogConfig(FrozenModel):
    api_url: str = "https://kubernetes.default.svc"
    ca_file: Path = SERVICE_ACCOUNT_DIR / "ca.crt"
    token_file: Path = SERVICE_ACCOUNT_DIR / "token"
    timeout_seconds: Annotated[float, Field(gt=0, le=30)] = 10.0


class _Body(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")


class _Meta(_Body):
    name: str


class _Pod(_Body):
    metadata: _Meta


class _PodList(_Body):
    items: tuple[_Pod, ...] = ()


class KubeLogReader:
    def __init__(self, http: httpx.AsyncClient) -> None:
        self._http = http

    @classmethod
    def in_cluster(cls, config: LogConfig) -> KubeLogReader:
        http = httpx.AsyncClient(
            base_url=config.api_url,
            verify=ssl.create_default_context(cafile=config.ca_file),
            auth=FileBearerAuth(config.token_file),
            timeout=config.timeout_seconds,
        )
        return cls(http)

    async def aclose(self) -> None:
        await self._http.aclose()

    async def pods(self, namespace: str, selector: str) -> list[str]:
        response = await self._get(
            f"/api/v1/namespaces/{namespace}/pods", {"labelSelector": selector}
        )
        try:
            return [p.metadata.name for p in _PodList.model_validate_json(response.content).items]
        except ValidationError as exc:
            raise DependencyUnavailableError("unexpected Pod list") from exc

    async def tail(self, namespace: str, pod: str, since_seconds: int) -> list[str]:
        response = await self._get(
            f"/api/v1/namespaces/{namespace}/pods/{pod}/log",
            {"sinceSeconds": str(since_seconds), "tailLines": str(MAX_LINES)},
        )
        return response.text.splitlines()

    async def _get(self, path: str, params: dict[str, str]) -> httpx.Response:
        try:
            response = await self._http.get(path, params=params)
        except httpx.HTTPError as exc:
            raise DependencyUnavailableError(f"Kubernetes GET {path} failed: {exc}") from exc
        if not response.is_success:
            raise DependencyUnavailableError(
                f"Kubernetes GET {path} returned {response.status_code}"
            )
        return response
