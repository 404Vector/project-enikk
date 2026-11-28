"""The HTTP application: the MCP endpoint and a health check."""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from mcp.server.transport_security import TransportSecuritySettings
from starlette.responses import JSONResponse

from enikk_drive.tools import build_server

if TYPE_CHECKING:
    from starlette.applications import Starlette
    from starlette.requests import Request

    from enikk_drive.service import ExchangeService

MCP_PATH: Final = "/mcp"
HEALTH_PATH: Final = "/healthz"


def create_app(service: ExchangeService, *, allowed_hosts: tuple[str, ...]) -> Starlette:
    server = build_server(service)
    server.custom_route(HEALTH_PATH, methods=["GET"], include_in_schema=False)(_health)
    security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=[pattern for host in allowed_hosts for pattern in (host, f"{host}:*")],
    )
    return server.streamable_http_app(
        streamable_http_path=MCP_PATH,
        stateless_http=True,
        json_response=True,
        transport_security=security,
    )


async def _health(_: Request) -> JSONResponse:
    return JSONResponse({"status": "ok"})
