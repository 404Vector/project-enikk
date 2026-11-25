"""The HTTP application: the MCP endpoint, a health check and the clock (ADR-0039 S5)."""

from __future__ import annotations

import asyncio
import contextlib
from typing import TYPE_CHECKING, Final

from mcp.server.transport_security import TransportSecuritySettings
from starlette.responses import JSONResponse

from enikk_scheduler.clock import Clock
from enikk_scheduler.tools import ScheduleReader, build_server

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from starlette.applications import Starlette
    from starlette.requests import Request

    from enikk_core_client import CoreClient

MCP_PATH: Final = "/mcp"
HEALTH_PATH: Final = "/healthz"


def create_app(
    core: CoreClient, *, allowed_hosts: tuple[str, ...], tick_seconds: float = 0
) -> Starlette:
    server = build_server(ScheduleReader(core))

    server.custom_route(HEALTH_PATH, methods=["GET"], include_in_schema=False)(_health)
    security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=[pattern for host in allowed_hosts for pattern in (host, f"{host}:*")],
    )
    app = server.streamable_http_app(
        streamable_http_path=MCP_PATH,
        stateless_http=True,
        json_response=True,
        transport_security=security,
    )
    if tick_seconds > 0:
        _with_clock(app, Clock(core, tick_seconds))
    return app


def _with_clock(app: Starlette, clock: Clock) -> None:
    """Run ``clock`` for as long as the application runs."""
    inner = app.router.lifespan_context

    @contextlib.asynccontextmanager
    async def lifespan(running: Starlette) -> AsyncIterator[None]:
        async with inner(running):
            task = asyncio.create_task(clock.run())
            try:
                yield
            finally:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task

    app.router.lifespan_context = lifespan


async def _health(_: Request) -> JSONResponse:
    return JSONResponse({"status": "ok"})
