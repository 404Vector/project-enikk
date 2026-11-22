"""Platform tools served in-process to the Claude CLI (ADR-0022).

They call core only, never an external MCP server, so they do not go through
agentgateway (I-05). Today there is one: ``delegate``.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Final

from claude_agent_sdk import create_sdk_mcp_server, tool
from pydantic import ValidationError

from enikk_contracts.tasks import DelegateRequest
from enikk_core_client.errors import CoreApiError, CoreUnavailableError

if TYPE_CHECKING:
    from claude_agent_sdk import McpSdkServerConfig, SdkMcpTool

    from enikk_runtime.model import EngineJob
    from enikk_runtime.ports import Delegator

SERVER: Final = "enikk"
DELEGATE_TOOL: Final = f"mcp__{SERVER}__delegate"

DELEGATE_DESCRIPTION: Final = (
    "Hand a change to the enikk platform itself - its code, configuration, agent "
    "definitions or documentation - to another agent. The developer agent works in an "
    "isolated workspace and opens a pull request; the owner approves it in Slack and "
    "nothing changes before that. Write a complete, self-contained request: the other "
    "agent does not see this conversation. Returns at once; its result is posted to "
    "this thread later. Call it at most once per message."
)

_log = logging.getLogger(__name__)


def granted_platform_tools(job: EngineJob) -> list[str]:
    return [DELEGATE_TOOL] if job.definition.delegates else []


def platform_server(job: EngineJob, delegator: Delegator) -> McpSdkServerConfig | None:
    if not job.definition.delegates:
        return None
    return create_sdk_mcp_server(SERVER, tools=[delegate_tool(job, delegator)])


def delegate_tool(job: EngineJob, delegator: Delegator) -> SdkMcpTool[Any]:
    agents = list(job.definition.delegates)
    schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "agent": {"type": "string", "enum": agents, "description": "Who should do it."},
            "request": {
                "type": "string",
                "description": "What to change and why, with every detail the agent needs.",
            },
        },
        "required": ["agent", "request"],
    }

    @tool("delegate", DELEGATE_DESCRIPTION, schema)
    async def delegate(args: dict[str, Any]) -> dict[str, Any]:
        try:
            request = DelegateRequest(
                lease_id=job.lease_id,
                agent=str(args.get("agent", "")),
                request=str(args.get("request", "")),
            )
        except ValidationError:
            return _result("The request was empty or too long (20000 characters at most).", True)
        try:
            accepted = await delegator.delegate_task(job.task_id, request)
        except CoreUnavailableError:
            return _result("The platform is unreachable right now; tell the owner.", True)
        except CoreApiError as exc:
            return _result(f"The delegation was refused: {exc}", True)
        _log.info("task %s delegated to %s as %s", job.task_id, accepted.agent, accepted.task_id)
        note = " (already delegated earlier in this message)" if accepted.duplicate else ""
        return _result(
            f"Accepted by {accepted.agent}{note}. Its result will be posted to this thread.",
            False,
        )

    return delegate


def _result(text: str, is_error: bool) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}], "is_error": is_error}
