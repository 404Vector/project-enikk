"""Base classes for service settings loaded from the environment."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict
from pydantic_settings import BaseSettings, SettingsConfigDict


class SettingsModel(BaseModel):
    """Base for nested settings groups.

    Not strict: environment values arrive as strings and must be coerced
    (e.g. ``"5432"`` to ``int``). Still immutable and closed to unknown fields.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")


class ServiceSettings(BaseSettings):
    """Immutable settings. Subclasses set ``env_prefix`` (e.g. ``ENIKK_CORE_``).

    Nested groups subclass ``SettingsModel`` and are read from
    ``<PREFIX><FIELD>__<SUBFIELD>`` environment variables.
    """

    model_config = SettingsConfigDict(
        env_nested_delimiter="__",
        extra="forbid",
        frozen=True,
    )
