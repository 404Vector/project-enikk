"""Connection settings and the OTLP exporter configuration derived from them."""

from __future__ import annotations

import base64
from typing import Final

from pydantic import Field, SecretStr

from enikk_kernel.models import FrozenModel

PROMPT_NAME_ATTRIBUTE: Final = "langfuse.observation.prompt.name"
PROMPT_VERSION_ATTRIBUTE: Final = "langfuse.observation.prompt.version"
SESSION_ID_ATTRIBUTE: Final = "langfuse.session.id"
"""Span attributes Langfuse maps onto prompts and sessions."""

INGESTION_VERSION: Final = "4"


class LangfuseConfig(FrozenModel):
    base_url: str = Field(pattern=r"^https?://[^/\s]+$")
    """Region host without a path, e.g. ``https://jp.cloud.langfuse.com``."""
    public_key: SecretStr
    secret_key: SecretStr

    def basic_token(self) -> SecretStr:
        pair = f"{self.public_key.get_secret_value()}:{self.secret_key.get_secret_value()}"
        return SecretStr(base64.b64encode(pair.encode()).decode())


class OtlpExport(FrozenModel):
    """OTLP/HTTP settings for a process that exports its own spans to Langfuse."""

    endpoint: str
    headers: SecretStr

    @classmethod
    def for_langfuse(cls, config: LangfuseConfig) -> OtlpExport:
        token = config.basic_token().get_secret_value()
        return cls(
            endpoint=f"{config.base_url}/api/public/otel",
            headers=SecretStr(
                f"Authorization=Basic {token},x-langfuse-ingestion-version={INGESTION_VERSION}"
            ),
        )

    def environment(self) -> dict[str, str]:
        """Standard ``OTEL_EXPORTER_OTLP_*`` variables. Holds the secret: pass to child
        processes only, never log it."""
        return {
            "OTEL_EXPORTER_OTLP_PROTOCOL": "http/protobuf",
            "OTEL_EXPORTER_OTLP_ENDPOINT": self.endpoint,
            "OTEL_EXPORTER_OTLP_HEADERS": self.headers.get_secret_value(),
        }
