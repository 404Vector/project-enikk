"""HTTP paths of the core API, shared by the server and the typed client."""

from __future__ import annotations

from typing import Final

HEALTH: Final = "/healthz"
EVENTS: Final = "/v1/events"
TASK_LEASE: Final = "/v1/tasks/lease"
TASK_CLAIM: Final = "/v1/tasks/{task_id}/claim"
TASK_DELEGATE: Final = "/v1/tasks/{task_id}/delegate"
TASK_HEARTBEAT: Final = "/v1/tasks/{task_id}/heartbeat"
TASK_RESULT: Final = "/v1/tasks/{task_id}/result"
TRANSCRIPT_APPEND: Final = "/v1/tasks/{task_id}/transcript/append"
TRANSCRIPT_LOAD: Final = "/v1/tasks/{task_id}/transcript/load"
TRANSCRIPT_SUBPATHS: Final = "/v1/tasks/{task_id}/transcript/subpaths"
OUTBOX_POLL: Final = "/v1/outbox/poll"
OUTBOX_ACK: Final = "/v1/outbox/ack"
CREDENTIALS: Final = "/v1/credentials"
NOTICES_POLL: Final = "/v1/notices/poll"
NOTICES_ACK: Final = "/v1/notices/ack"
APPROVAL_DECISION: Final = "/v1/approvals/{approval_id}/decision"
REVERTS: Final = "/v1/reverts"
MEMORY_REMEMBER: Final = "/v1/tasks/{task_id}/memory/remember"
MEMORY_UPDATE: Final = "/v1/tasks/{task_id}/memory/update"
MEMORY_FORGET: Final = "/v1/tasks/{task_id}/memory/forget"
MEMORY_SEARCH: Final = "/v1/tasks/{task_id}/memory/search"
REVIEW_THREADS: Final = "/v1/review-threads"
GITHUB_HEALTH: Final = "/v1/github/health"
GITHUB_TOKEN: Final = "/v1/github/token"  # noqa: S105 - URL path

AUDIENCE: Final = "enikk-core"
"""Audience of the ServiceAccount tokens that core accepts (ADR-0011)."""
