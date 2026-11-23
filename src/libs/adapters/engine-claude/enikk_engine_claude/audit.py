"""Records what the model does outside the conversation (ADR-0033).

Every shell command, web search and web fetch is logged with the role and the task,
secrets masked. Fetching a cluster-internal or private address is refused: what the
web returns is untrusted input, and the cluster's services are not the web.
"""

from __future__ import annotations

import ipaddress
import logging
from typing import TYPE_CHECKING, Any, Final, cast
from urllib.parse import urlsplit

from claude_agent_sdk import HookMatcher

from enikk_kernel.redaction import redact

if TYPE_CHECKING:
    from claude_agent_sdk.types import HookContext, HookEvent, HookInput, HookJSONOutput

    from enikk_runtime.model import EngineJob

AUDITED: Final = ("Bash", "Monitor", "WebFetch", "WebSearch")
MAX_LOGGED: Final = 2_000
INTERNAL_SUFFIXES: Final = (".svc", ".cluster.local", ".local", ".internal", ".localhost")

audit_log = logging.getLogger("enikk_engine_claude.audit")


def internal_url(url: str) -> bool:
    """Whether ``url`` points into the cluster, the Pod or a private network."""
    try:
        host = (urlsplit(url).hostname or "").rstrip(".").lower()
    except ValueError:
        return True
    if not host or host == "localhost" or "." not in host:
        return True
    if host.endswith(INTERNAL_SUFFIXES):
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    return not address.is_global


def described(tool: str, tool_input: dict[str, Any]) -> str:
    """The part of a tool call worth recording."""
    keys = {"WebFetch": "url", "WebSearch": "query"}.get(tool)
    if keys is not None:
        return str(tool_input.get(keys, ""))
    for key in ("command", "url", "query"):
        if key in tool_input:
            return str(tool_input[key])
    return str(tool_input)


def audit_hooks(job: EngineJob) -> dict[HookEvent, list[HookMatcher]]:
    """PreToolUse hooks for ``job``. A failing hook blocks the tool instead of skipping
    the record (``on_failure="block"``)."""

    async def audit(
        data: HookInput, tool_use_id: str | None, context: HookContext
    ) -> HookJSONOutput:
        fields = cast("dict[str, Any]", data)
        tool = str(fields.get("tool_name", ""))
        tool_input = fields.get("tool_input")
        detail = described(tool, tool_input if isinstance(tool_input, dict) else {})
        audit_log.info(
            "tool %s role %s task %s: %s",
            tool,
            job.definition.name,
            job.task_id,
            redact(detail)[:MAX_LOGGED],
        )
        if tool == "WebFetch" and internal_url(detail):
            audit_log.warning(
                "tool WebFetch role %s task %s: refused an internal address",
                job.definition.name,
                job.task_id,
            )
            return {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": (
                        "Cluster-internal and private addresses are not fetched."
                    ),
                }
            }
        return {}

    return {
        "PreToolUse": [HookMatcher(matcher="|".join(AUDITED), hooks=[audit], on_failure="block")]
    }
