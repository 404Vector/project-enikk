"""The scheduler's MCP tools (ADR-0039 §4). S2 has the read tools only."""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Any, Final

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp_types import ToolAnnotations
from pydantic import Field

from enikk_contracts.schedules import RunsQuery, ScheduleName, ScheduleQuery
from enikk_core_client.errors import CoreApiError
from enikk_scheduler.caller import MissingCallerError, caller_from

if TYPE_CHECKING:
    from collections.abc import Awaitable

    from enikk_contracts.base import Contract
    from enikk_contracts.schedules import Caller, RunList, ScheduleList, ScheduleView
    from enikk_core_client import CoreClient

SERVER_NAME: Final = "enikk-scheduler"
READ_ONLY: Final = ToolAnnotations(
    read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False
)


class ScheduleReader:
    """Reads schedules from core for one calling task."""

    def __init__(self, core: CoreClient) -> None:
        self._core = core

    async def list_schedules(self, caller: Caller) -> ScheduleList:
        return await self._core.list_schedules(ScheduleQuery(caller=caller))

    async def get_schedule(self, caller: Caller, name: str) -> ScheduleView:
        return await self._core.get_schedule(name, ScheduleQuery(caller=caller))

    async def list_runs(self, caller: Caller, name: str, limit: int) -> RunList:
        return await self._core.list_runs(name, RunsQuery(caller=caller, limit=limit))


def build_server(reader: ScheduleReader) -> MCPServer:
    server = MCPServer(SERVER_NAME, instructions="Schedules of enikk's agents (ADR-0039).")

    @server.tool(annotations=READ_ONLY)
    async def list_schedules(ctx: Context) -> dict[str, Any]:
        """List all schedules with their state, agent and timing."""
        caller = _caller(ctx)
        return await _call(reader.list_schedules(caller))

    @server.tool(annotations=READ_ONLY)
    async def get_schedule(name: ScheduleName, ctx: Context) -> dict[str, Any]:
        """Show one schedule by name."""
        caller = _caller(ctx)
        return await _call(reader.get_schedule(caller, name))

    @server.tool(annotations=READ_ONLY)
    async def list_runs(
        name: ScheduleName,
        ctx: Context,
        limit: Annotated[int, Field(ge=1, le=100)] = 20,
    ) -> dict[str, Any]:
        """List the latest runs of one schedule, newest first."""
        caller = _caller(ctx)
        return await _call(reader.list_runs(caller, name, limit))

    return server


def _caller(ctx: Context) -> Caller:
    try:
        return caller_from(ctx.headers)
    except MissingCallerError as exc:
        raise ToolError(str(exc)) from exc


async def _call(pending: Awaitable[Contract]) -> dict[str, Any]:
    try:
        result = await pending
    except CoreApiError as exc:
        raise ToolError(str(exc)) from exc
    dumped: dict[str, Any] = result.model_dump(mode="json")
    return dumped
