"""HTTP paths of the core API, shared by the server and the typed client."""

from __future__ import annotations

from typing import Final

HEALTH: Final = "/healthz"
EVENTS: Final = "/v1/events"
TASK_LEASE: Final = "/v1/tasks/lease"
TASK_CLAIM: Final = "/v1/tasks/{task_id}/claim"
TASK_HEARTBEAT: Final = "/v1/tasks/{task_id}/heartbeat"
TASK_RESULT: Final = "/v1/tasks/{task_id}/result"
TRANSCRIPT_APPEND: Final = "/v1/tasks/{task_id}/transcript/append"
TRANSCRIPT_LOAD: Final = "/v1/tasks/{task_id}/transcript/load"
TRANSCRIPT_SUBPATHS: Final = "/v1/tasks/{task_id}/transcript/subpaths"
OUTBOX_POLL: Final = "/v1/outbox/poll"
OUTBOX_ACK: Final = "/v1/outbox/ack"
CREDENTIALS: Final = "/v1/credentials"
GITHUB_HEALTH: Final = "/v1/github/health"
GITHUB_TOKEN: Final = "/v1/github/token"  # noqa: S105 - URL path

AUDIENCE: Final = "enikk-core"
"""Audience of the ServiceAccount tokens that core accepts (ADR-0011)."""
