"""Services ↔ core: credentials handed out by core (ADR-0013)."""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Self

from pydantic import Field, PlainSerializer, SecretStr, model_validator

from enikk_contracts.base import Contract


class CredentialName(StrEnum):
    """Every credential the platform knows. Values are never stored in git."""

    SLACK_BOT_TOKEN = "slack.bot-token"
    SLACK_APP_TOKEN = "slack.app-token"
    CLAUDE_OAUTH_TOKEN = "claude.oauth-token"
    LANGFUSE_PUBLIC_KEY = "langfuse.public-key"
    LANGFUSE_SECRET_KEY = "langfuse.secret-key"


def _reveal(value: SecretStr) -> str:
    return value.get_secret_value()


CredentialValue = Annotated[
    SecretStr,
    Field(min_length=1, max_length=16_384),
    PlainSerializer(_reveal, return_type=str, when_used="json"),
]
"""Masked in ``repr`` and Python dumps; written in clear only to JSON."""


class CredentialRequest(Contract):
    names: Annotated[tuple[CredentialName, ...], Field(min_length=1)]

    @model_validator(mode="after")
    def _unique(self) -> Self:
        if len(set(self.names)) != len(self.names):
            raise ValueError("credential names must be unique")
        return self


class Credentials(Contract):
    values: dict[CredentialName, CredentialValue]
