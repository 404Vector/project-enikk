from __future__ import annotations

import uuid

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from enikk_langfuse.tracing import LangfuseTracing
from enikk_runtime.model import EngineJob, EngineResult, TokenUsage
from enikk_runtime.testing import definition, prompt

pytestmark = pytest.mark.anyio


def _job() -> EngineJob:
    return EngineJob(
        task_id=uuid.uuid4(),
        lease_id=uuid.uuid4(),
        attempt=1,
        session_id=uuid.uuid4(),
        engine_session_id=None,
        definition=definition(),
        prompt=prompt(),
        input="hello",
    )


def _tracing() -> tuple[LangfuseTracing, InMemorySpanExporter]:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return LangfuseTracing(provider), exporter


async def test_success_is_a_generation_linked_to_the_prompt() -> None:
    tracing, exporter = _tracing()
    job = _job()
    async with tracing.observer().observe(job) as observation:
        observation.succeeded(
            EngineResult(
                text="hi!",
                model="claude-test-1-20261101",
                usage=TokenUsage(input_tokens=10, output_tokens=3, cache_read_tokens=100),
            )
        )
    generation, task = exporter.get_finished_spans()
    assert task.name == "enikk.task"
    assert generation.parent is not None
    assert generation.parent.span_id == task.context.span_id
    g = dict(generation.attributes or {})
    assert g["langfuse.observation.type"] == "generation"
    assert g["langfuse.observation.prompt.name"] == "assistant"
    assert g["langfuse.observation.prompt.version"] == 3
    assert g["langfuse.observation.model.name"] == "claude-test-1-20261101"
    assert g["langfuse.observation.output"] == "hi!"
    assert '"cache_read_input_tokens":100' in str(g["langfuse.observation.usage_details"])
    t = dict(task.attributes or {})
    assert t["langfuse.session.id"] == str(job.session_id)
    assert t["langfuse.trace.name"] == "assistant"
    assert t["langfuse.trace.output"] == "hi!"


async def test_failure_marks_both_spans_as_errors() -> None:
    tracing, exporter = _tracing()
    async with tracing.observer().observe(_job()) as observation:
        observation.failed("rate limited")
    for span in exporter.get_finished_spans():
        attributes = dict(span.attributes or {})
        assert attributes["langfuse.observation.level"] == "ERROR"
        assert attributes["langfuse.observation.status_message"] == "rate limited"
        assert not span.status.is_ok


async def test_loaded_skills_are_in_the_trace_metadata() -> None:
    from enikk_runtime.model import SkillUse

    tracing, exporter = _tracing()
    async with tracing.observer().observe(_job()) as observation:
        observation.succeeded(
            EngineResult(text="hi", skills=(SkillUse(name="skill-developer", version=4),))
        )
    generation, task = exporter.get_finished_spans()
    expected = '[{"name":"skill-developer","version":4}]'
    assert dict(task.attributes or {})["langfuse.trace.metadata.skills"] == expected
    assert dict(generation.attributes or {})["langfuse.observation.metadata.skills"] == expected
