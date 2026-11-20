"""HTTP paths of the core API, shared by the server and the typed client."""

from __future__ import annotations

from typing import Final

HEALTH: Final = "/healthz"
EVENTS: Final = "/v1/events"
TASK_LEASE: Final = "/v1/tasks/lease"
TASK_HEARTBEAT: Final = "/v1/tasks/{task_id}/heartbeat"
TASK_RESULT: Final = "/v1/tasks/{task_id}/result"
OUTBOX_POLL: Final = "/v1/outbox/poll"
OUTBOX_ACK: Final = "/v1/outbox/ack"

AUDIENCE: Final = "enikk-core"
"""Audience of the ServiceAccount tokens that core accepts (ADR-0011)."""
