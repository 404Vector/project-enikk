"""MCP tools through agentgateway (ADR-0039 §2, I-05, I-14).

Each server of the definition's ``tools.mcp`` becomes one HTTP MCP server of the CLI at
``<gateway>/mcp/<server>``; the CLI never learns a server's own address. The request
carries core's token for the task. The CLI reads it from its environment
(``${ENIKK_MCP_TOKEN}`` in the header), so it is not in the CLI's arguments, and the
sandboxed shell drops it with the other secret-looking variables. The gateway checks
the token, lists and allows only the tools it names, and hands the task and lease to
the server. The engine allows only the granted tools as well.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Final

from claude_agent_sdk.types import McpHttpServerConfig

if TYPE_CHECKING:
    from enikk_runtime.model import EngineJob

ENV_TOKEN: Final = "ENIKK_MCP_TOKEN"  # noqa: S105 - variable name
"""Holds the task's MCP token for the CLI. Its name matches the sandbox's secret
pattern, so shell commands never see it."""
PATH_PREFIX: Final = "/mcp/"

_log = logging.getLogger(__name__)


class GatewayTools:
    """The MCP servers, tool names and environment of one run."""

    def __init__(
        self,
        servers: dict[str, McpHttpServerConfig],
        allowed: list[str],
        env: dict[str, str],
    ) -> None:
        self.servers = servers
        self.allowed = allowed
        self.env = env


NONE: Final = GatewayTools({}, [], {})


def gateway_tools(job: EngineJob, gateway: str | None, *, reserved: str) -> GatewayTools:
    """Nothing when the gateway is off, the definition grants no MCP tool, or core gave
    no token (no signing key)."""
    grants = job.definition.tools.mcp
    if gateway is None or not grants:
        return NONE
    if job.mcp_token is None:
        _log.warning("task %s: no MCP token from core; running without MCP tools", job.task_id)
        return NONE
    base = gateway.rstrip("/")
    servers: dict[str, McpHttpServerConfig] = {}
    allowed: list[str] = []
    for grant in grants:
        if grant.server == reserved:
            _log.error("task %s: MCP server name %r is reserved", job.task_id, grant.server)
            continue
        servers[grant.server] = McpHttpServerConfig(
            type="http",
            url=f"{base}{PATH_PREFIX}{grant.server}",
            headers={"Authorization": f"Bearer ${{{ENV_TOKEN}}}"},
        )
        allowed.extend(tool_name(grant.server, tool) for tool in grant.allow)
    if not servers:
        return NONE
    return GatewayTools(servers, allowed, {ENV_TOKEN: job.mcp_token.get_secret_value()})


def tool_name(server: str, tool: str) -> str:
    """The CLI's name for an MCP tool."""
    return f"mcp__{server}__{tool}"
