"""``RunObserver`` that sends one trace per task to Langfuse over OTLP (ADR-0016).

Each task becomes a ``enikk.task`` span with a ``generation`` child linked to the
prompt version, so Langfuse shows input, output, model, usage and prompt per run.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Final, Self

import pydantic_core
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.trace import Status, StatusCode

from enikk_langfuse.config import (
    INGESTION_VERSION,
    PROMPT_NAME_ATTRIBUTE,
    PROMPT_VERSION_ATTRIBUTE,
    SESSION_ID_ATTRIBUTE,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from opentelemetry.trace import Span, Tracer

    from enikk_langfuse.config import LangfuseConfig
    from enikk_runtime.model import EngineJob, EngineResult

TRACE_NAME: Final = "langfuse.trace.name"
TRACE_INPUT: Final = "langfuse.trace.input"
TRACE_OUTPUT: Final = "langfuse.trace.output"
OBSERVATION_TYPE: Final = "langfuse.observation.type"
OBSERVATION_INPUT: Final = "langfuse.observation.input"
OBSERVATION_OUTPUT: Final = "langfuse.observation.output"
OBSERVATION_MODEL: Final = "langfuse.observation.model.name"
OBSERVATION_PARAMETERS: Final = "langfuse.observation.model.parameters"
OBSERVATION_USAGE: Final = "langfuse.observation.usage_details"
OBSERVATION_LEVEL: Final = "langfuse.observation.level"
OBSERVATION_STATUS: Final = "langfuse.observation.status_message"
TRACE_SKILLS: Final = "langfuse.trace.metadata.skills"
OBSERVATION_SKILLS: Final = "langfuse.observation.metadata.skills"


class LangfuseTracing:
    """Owns a private ``TracerProvider``; never touches the global OTel state."""

    def __init__(self, provider: TracerProvider) -> None:
        self._provider = provider
        self._tracer = provider.get_tracer("enikk.runtime")

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

    def observer(self) -> LangfuseRunObserver:
        return LangfuseRunObserver(self._tracer)

    def shutdown(self) -> None:
        """Flush pending spans. Call once at process exit."""
        self._provider.shutdown()


class _Generation:
    def __init__(self, task: Span, generation: Span, job: EngineJob) -> None:
        self._task = task
        self._generation = generation
        self._job = job

    def succeeded(self, result: EngineResult) -> None:
        self._generation.set_attribute(OBSERVATION_OUTPUT, result.text)
        self._generation.set_attribute(OBSERVATION_MODEL, result.model or self._job.prompt.model)
        if result.usage is not None:
            usage = {
                "input": result.usage.input_tokens,
                "output": result.usage.output_tokens,
                "cache_read_input_tokens": result.usage.cache_read_tokens,
                "cache_creation_input_tokens": result.usage.cache_creation_tokens,
            }
            self._generation.set_attribute(OBSERVATION_USAGE, _json(usage))
        self._task.set_attribute(TRACE_OUTPUT, result.text)
        if result.skills:
            # Skills the run loaded with load_skill (ADR-0037 §3).
            skills = _json([s.model_dump() for s in result.skills])
            self._task.set_attribute(TRACE_SKILLS, skills)
            self._generation.set_attribute(OBSERVATION_SKILLS, skills)

    def failed(self, reason: str) -> None:
        for span in (self._generation, self._task):
            span.set_attribute(OBSERVATION_LEVEL, "ERROR")
            span.set_attribute(OBSERVATION_STATUS, reason)
            span.set_status(Status(StatusCode.ERROR, reason))


class LangfuseRunObserver:
    def __init__(self, tracer: Tracer) -> None:
        self._tracer = tracer

    @asynccontextmanager
    async def observe(self, job: EngineJob) -> AsyncIterator[_Generation]:
        session = str(job.session_id)
        task_attributes: dict[str, str | int] = {
            TRACE_NAME: job.definition.name,
            TRACE_INPUT: job.input,
            SESSION_ID_ATTRIBUTE: session,
            "enikk.task.id": str(job.task_id),
            "enikk.task.attempt": job.attempt,
        }
        generation_attributes: dict[str, str | int] = {
            OBSERVATION_TYPE: "generation",
            OBSERVATION_INPUT: job.input,
            OBSERVATION_MODEL: job.prompt.model,
            OBSERVATION_PARAMETERS: _json(job.prompt.parameters),
            PROMPT_NAME_ATTRIBUTE: job.prompt.name,
            PROMPT_VERSION_ATTRIBUTE: job.prompt.version,
            SESSION_ID_ATTRIBUTE: session,
        }
        with (
            self._tracer.start_as_current_span("enikk.task", attributes=task_attributes) as task,
            self._tracer.start_as_current_span(
                job.definition.engine.value, attributes=generation_attributes
            ) as generation,
        ):
            yield _Generation(task, generation, job)


def _json(value: object) -> str:
    return pydantic_core.to_json(value).decode()
