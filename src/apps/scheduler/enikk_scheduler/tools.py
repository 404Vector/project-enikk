"""The scheduler's MCP tools (ADR-0039 §4): read and change schedules. Nothing waits
for approval; a schedule's tools never exceed its agent's definition (ADR-0041 §1)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Any, Final

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp_types import ToolAnnotations
from pydantic import Field, ValidationError, WithJsonSchema

from enikk_contracts.schedules import (
    RunNow,
    RunsQuery,
    ScheduleCreate,
    ScheduleName,
    SchedulePause,
    ScheduleQuery,
    ScheduleSpec,
    ScheduleUpdate,
)
from enikk_core_client.errors import CoreApiError
from enikk_scheduler.caller import MissingCallerError, caller_from

if TYPE_CHECKING:
    from collections.abc import Awaitable

    from enikk_contracts.base import Contract
    from enikk_contracts.schedules import (
        Caller,
        RunList,
        RunView,
        ScheduleDeleted,
        ScheduleList,
        ScheduleView,
    )
    from enikk_core_client import CoreClient


def _inlined(schema: dict[str, Any]) -> dict[str, Any]:
    """``schema`` with its ``$defs`` references written out, so it can sit inside another
    schema (a tool's input schema has its own root)."""
    defs: dict[str, Any] = schema.get("$defs", {})

    def resolve(node: Any) -> Any:  # noqa: ANN401 - any JSON value
        if isinstance(node, dict):
            ref = node.get("$ref")
            if isinstance(ref, str) and ref.startswith("#/$defs/"):
                return resolve(defs[ref.removeprefix("#/$defs/")])
            return {k: resolve(v) for k, v in node.items() if k != "$defs"}
        if isinstance(node, list):
            return [resolve(v) for v in node]
        return node

    resolved: dict[str, Any] = resolve(schema)
    return resolved


SpecArgument = Annotated[dict[str, Any], WithJsonSchema(_inlined(ScheduleSpec.model_json_schema()))]
"""A spec in the tool's input schema; ``_spec`` reads it."""

SERVER_NAME: Final = "enikk-scheduler"
READ_ONLY: Final = ToolAnnotations(
    read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False
)
CHANGES: Final = ToolAnnotations(
    read_only_hint=False, destructive_hint=False, idempotent_hint=True, open_world_hint=False
)
REMOVES: Final = ToolAnnotations(
    read_only_hint=False, destructive_hint=True, idempotent_hint=False, open_world_hint=False
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

    async def create(self, caller: Caller, name: str, spec: ScheduleSpec) -> ScheduleView:
        return await self._core.create_schedule(ScheduleCreate(caller=caller, name=name, spec=spec))

    async def update(self, caller: Caller, name: str, spec: ScheduleSpec) -> ScheduleView:
        return await self._core.update_schedule(name, ScheduleUpdate(caller=caller, spec=spec))

    async def pause(self, caller: Caller, name: str, paused: bool) -> ScheduleView:
        return await self._core.pause_schedule(name, SchedulePause(caller=caller, paused=paused))

    async def delete(self, caller: Caller, name: str) -> ScheduleDeleted:
        return await self._core.delete_schedule(name, ScheduleQuery(caller=caller))

    async def run_now(self, caller: Caller, name: str) -> RunView:
        return await self._core.run_schedule_now(name, RunNow(caller=caller))


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

    @server.tool(annotations=CHANGES)
    async def create_schedule(
        name: ScheduleName, spec: SpecArgument, ctx: Context
    ) -> dict[str, Any]:
        """Create a schedule. spec: description, timing (cron 5 fields with a timezone,
        or once at a time), agent, an optional skill loaded first, tools (within the
        agent's definition), input (text, or day-conversations), output (owner-dm,
        thread, vault-note with a folder, channel with a channel name such as general,
        discard), catchup and retries. Active at once."""
        caller = _caller(ctx)
        return await _call(reader.create(caller, name, _spec(spec)))

    @server.tool(annotations=CHANGES)
    async def update_schedule(
        name: ScheduleName, spec: SpecArgument, ctx: Context
    ) -> dict[str, Any]:
        """Replace the whole spec of a schedule (get_schedule first to keep the rest)."""
        caller = _caller(ctx)
        return await _call(reader.update(caller, name, _spec(spec)))

    @server.tool(annotations=CHANGES)
    async def pause_schedule(name: ScheduleName, ctx: Context) -> dict[str, Any]:
        """Pause a schedule; it makes no runs until resumed."""
        caller = _caller(ctx)
        return await _call(reader.pause(caller, name, paused=True))

    @server.tool(annotations=CHANGES)
    async def resume_schedule(name: ScheduleName, ctx: Context) -> dict[str, Any]:
        """Resume a paused schedule."""
        caller = _caller(ctx)
        return await _call(reader.pause(caller, name, paused=False))

    @server.tool(annotations=CHANGES)
    async def run_now(name: ScheduleName, ctx: Context) -> dict[str, Any]:
        """Start one run of a schedule now, whatever its timing; its output goes where
        the schedule says. list_runs shows how it went."""
        caller = _caller(ctx)
        return await _call(reader.run_now(caller, name))

    @server.tool(annotations=REMOVES)
    async def delete_schedule(name: ScheduleName, ctx: Context) -> dict[str, Any]:
        """Delete a schedule and its run history."""
        caller = _caller(ctx)
        return await _call(reader.delete(caller, name))

    return server


def _spec(raw: dict[str, Any]) -> ScheduleSpec:
    """The spec as sent: JSON strings for enums and times, read the lenient way. The
    contract is strict for Python callers; a tool argument is plain JSON."""
    try:
        return ScheduleSpec.model_validate(raw, strict=False)
    except ValidationError as exc:
        raise ToolError(f"invalid spec: {exc}") from exc


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
