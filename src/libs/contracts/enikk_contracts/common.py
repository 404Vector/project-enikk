"""Vocabulary shared by several contracts."""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated

from pydantic import StringConstraints

from enikk_contracts.base import Contract

AgentName = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9-]{0,62}$")]
"""Agent definition identifier: lowercase letters, digits and hyphens."""

NonEmptyText = Annotated[str, StringConstraints(min_length=1, max_length=40_000)]
"""Free text exchanged with users (messages, results)."""

Identifier = Annotated[str, StringConstraints(min_length=1, max_length=255)]
"""Opaque identifier issued by an external system (Slack ids, engine session ids)."""


class Engine(StrEnum):
    CLAUDE = "claude"
    GEMINI = "gemini"


class ConnectorKind(StrEnum):
    SLACK = "slack"


class ConversationRef(Contract):
    """Location of a conversation thread in an external interface."""

    connector: ConnectorKind
    channel: Identifier
    thread: Identifier
