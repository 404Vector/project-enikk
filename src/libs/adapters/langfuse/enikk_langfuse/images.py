"""Traces of the image tool server (ADR-0043): one ``generation`` per image request.

Each request is its own trace in the calling task's Langfuse session, with model,
parameters, token usage and cost. The image bytes are never sent; the output is the
saved file's path.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Self

import pydantic_core
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.trace import Status, StatusCode
from pydantic import Field, NonNegativeFloat, NonNegativeInt

from enikk_kernel.models import FrozenModel
from enikk_langfuse.config import (
    INGESTION_VERSION,
    PROMPT_NAME_ATTRIBUTE,
    PROMPT_VERSION_ATTRIBUTE,
    SESSION_ID_ATTRIBUTE,
)
from enikk_langfuse.tracing import (
    OBSERVATION_INPUT,
    OBSERVATION_LEVEL,
    OBSERVATION_MODEL,
    OBSERVATION_OUTPUT,
    OBSERVATION_PARAMETERS,
    OBSERVATION_STATUS,
    OBSERVATION_TYPE,
    OBSERVATION_USAGE,
    TRACE_INPUT,
    TRACE_NAME,
    TRACE_OUTPUT,
)

if TYPE_CHECKING:
    from opentelemetry.trace import Tracer

    from enikk_langfuse.config import LangfuseConfig

OBSERVATION_COST = "langfuse.observation.cost_details"


def _json(value: object) -> str:
    return pydantic_core.to_json(value).decode()


class ImageRecord(FrozenModel):
    """One image request as the server saw it."""

    session_id: uuid.UUID
    tool: str = Field(min_length=1)
    prompt: str
    model: str = Field(min_length=1)
    parameters: dict[str, str]
    references: NonNegativeInt = 0
    input_tokens: NonNegativeInt | None = None
    output_tokens: NonNegativeInt | None = None
    cost_usd: NonNegativeFloat | None = None
    output: str | None = None
    """Where the image was saved, or the model's text when it made none."""
    error: str | None = None
    style: str | None = None
    """The style prompt (``style-*``) put before the request, linked to its version."""
    style_version: int | None = None


class ImageTracing:
    """Owns a private ``TracerProvider``; never touches the global OTel state."""

    def __init__(self, provider: TracerProvider) -> None:
        self._provider = provider
        self._tracer: Tracer = provider.get_tracer("enikk.imagegen")

    @classmethod
    def connect(cls, config: LangfuseConfig, *, service_name: str) -> Self:
        exporter = OTLPSpanExporter(
            endpoint=f"{config.base_url}/api/public/otel/v1/traces",
            headers={
                "Authorization": f"Basic {config.basic_token().get_secret_value()}",
                "x-langfuse-ingestion-version": INGESTION_VERSION,
            },
        )
        provider = TracerProvider(resource=Resource.create({"service.name": service_name}))
        provider.add_span_processor(BatchSpanProcessor(exporter))
        return cls(provider)

    def record(self, record: ImageRecord) -> None:
        trace_attributes: dict[str, str] = {
            TRACE_NAME: f"imagegen.{record.tool}",
            TRACE_INPUT: record.prompt,
            SESSION_ID_ATTRIBUTE: str(record.session_id),
        }
        with (
            self._tracer.start_as_current_span(
                f"imagegen.{record.tool}", attributes=trace_attributes
            ) as trace,
            self._tracer.start_as_current_span(
                "generation", attributes={OBSERVATION_TYPE: "generation"}
            ) as generation,
        ):
            generation.set_attribute(OBSERVATION_INPUT, record.prompt)
            generation.set_attribute(OBSERVATION_MODEL, record.model)
            if record.style is not None and record.style_version is not None:
                generation.set_attribute(PROMPT_NAME_ATTRIBUTE, record.style)
                generation.set_attribute(PROMPT_VERSION_ATTRIBUTE, record.style_version)
            parameters = {**record.parameters, "references": str(record.references)}
            generation.set_attribute(OBSERVATION_PARAMETERS, _json(parameters))
            usage = {
                key: value
                for key, value in (
                    ("input", record.input_tokens),
                    ("output", record.output_tokens),
                )
                if value is not None
            }
            if usage:
                generation.set_attribute(OBSERVATION_USAGE, _json(usage))
            if record.cost_usd is not None:
                generation.set_attribute(OBSERVATION_COST, _json({"total": record.cost_usd}))
            if record.output is not None:
                generation.set_attribute(OBSERVATION_OUTPUT, record.output)
                trace.set_attribute(TRACE_OUTPUT, record.output)
            if record.error is not None:
                for span in (generation, trace):
                    span.set_attribute(OBSERVATION_LEVEL, "ERROR")
                    span.set_attribute(OBSERVATION_STATUS, record.error)
                    span.set_status(Status(StatusCode.ERROR, record.error))

    def shutdown(self) -> None:
        """Flush pending spans. Call once at process exit."""
        self._provider.shutdown()
