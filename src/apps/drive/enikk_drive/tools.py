"""The exchange MCP tools (ADR-0052): put a thread's file into the Drive exchange
folder, get one into the thread's tmp, list a folder. File bytes never pass through
the tool results; only paths, sizes and times do."""

from __future__ import annotations

import logging
import uuid
from typing import TYPE_CHECKING, Annotated, Any, Final

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp_types import ToolAnnotations
from pydantic import Field

from enikk_drive.tokens import TokenRejectedError
from enikk_kernel.errors import EnikkError

if TYPE_CHECKING:
    from collections.abc import Awaitable

    from enikk_drive.service import ExchangeService
    from enikk_drive.tokens import TokenVerifier
    from enikk_kernel.models import FrozenModel

_log = logging.getLogger(__name__)

SERVER_NAME: Final = "enikk-drive"
WRITES: Final = ToolAnnotations(
    read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=True
)
READS: Final = ToolAnnotations(read_only_hint=True, open_world_hint=True)
DrivePathText = Annotated[str, Field(max_length=2048)]


def build_server(service: ExchangeService, verifier: TokenVerifier) -> MCPServer:
    root = service.root_folder
    server = MCPServer(
        SERVER_NAME,
        instructions=(
            f"File exchange through the Google Drive folder '{root}' at the top of the "
            "owner's My Drive (ADR-0052). The owner's Windows PC syncs that folder with "
            "Drive for Desktop, so the Unity tools can read and write it as a local folder. "
            "Drive paths are inside that folder, e.g. to-unity/hero.png; nothing outside "
            "it is reached. Syncing to the PC takes seconds to minutes. File bytes never "
            "pass through tool results."
        ),
    )

    @server.tool(annotations=WRITES)
    async def drive_put(
        path: str, ctx: Context, dest: DrivePathText = "", overwrite: bool = False
    ) -> dict[str, Any]:
        """Upload one of this thread's files (under its tmp or worktree) into the
        exchange folder. dest: the Drive path; empty puts the file at the top, a path
        ending in / is a folder that keeps the file name. Missing folders are created.
        An existing file is replaced only with overwrite=true (Drive keeps the old
        revision)."""
        session = await _session(verifier, ctx, "drive_put")
        return await _call(service.put(session, path, dest, overwrite=overwrite))

    @server.tool(annotations=WRITES)
    async def drive_get(path: DrivePathText, ctx: Context) -> dict[str, Any]:
        """Download a file from the exchange folder into this thread's tmp, under
        drive/ with the same folders. Returns the local path, ready for send_file or
        other tools. Google Docs, folders and shortcuts cannot be got."""
        session = await _session(verifier, ctx, "drive_get")
        return await _call(service.get(session, path))

    @server.tool(annotations=READS)
    async def drive_list(ctx: Context, folder: DrivePathText = "") -> dict[str, Any]:
        """List one folder of the exchange folder (empty: its top): names, kinds,
        sizes and modified times, at most 200. Use it to see whether a file from the
        PC has synced."""
        await _session(verifier, ctx, "drive_list")
        return await _call(service.list_folder(folder))

    return server


async def _session(verifier: TokenVerifier, ctx: Context, tool: str) -> uuid.UUID:
    """The calling session, from core's task token once it is checked and names the tool
    (ADR-0052 §4)."""
    try:
        claims = await verifier.verify(ctx.headers)
        if not claims.allows(tool):
            raise TokenRejectedError(f"the task's token does not allow {tool}")
        return uuid.UUID(claims.session)
    except TokenRejectedError as exc:
        _log.warning("refused %s: %s", tool, exc)
        raise ToolError(str(exc)) from exc
    except ValueError as exc:
        raise ToolError("the task token's session is not a UUID") from exc


async def _call(pending: Awaitable[FrozenModel]) -> dict[str, Any]:
    try:
        result = await pending
    except EnikkError as exc:
        raise ToolError(str(exc)) from exc
    dumped: dict[str, Any] = result.model_dump(mode="json")
    return dumped
