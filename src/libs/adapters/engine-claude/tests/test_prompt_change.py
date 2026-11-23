"""A promoted prompt reaches threads that started before it (ADR-0032 §7).

Claude Code saves the system prompt in the session transcript (``prompt_snapshot``) and
uses that saved one again on ``--resume``, ignoring the new ``--system-prompt``. These
tests run the real CLI against a fake Anthropic API on localhost, with transcripts kept
the way core keeps them, and look at the system prompt the API receives.
"""

from __future__ import annotations

import http.server
import threading
import uuid
from typing import TYPE_CHECKING, Any

import pydantic_core
import pytest
from pydantic import SecretStr

from enikk_contracts.transcripts import (
    SubpathQuery,
    TranscriptAppend,
    TranscriptAppended,
    TranscriptEntry,
    TranscriptLoaded,
    TranscriptQuery,
    TranscriptSubpaths,
)
from enikk_engine_claude.engine import ClaudeEngine, ClaudeEngineConfig
from enikk_engine_claude.resume import (
    PROMPT_CHANGED_EMPTY_HEADER,
    PROMPT_CHANGED_HEADER,
    prompt_changed,
    session_prompt,
    with_history,
)
from enikk_runtime.model import EngineJob, ResolvedPrompt
from enikk_runtime.testing import definition

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

pytestmark = pytest.mark.anyio

OLD = "You are enikk. OLD-PROMPT-V4"
NEW = "You are enikk. NEW-PROMPT-V6"


class Api:
    """Answers "ok"; keeps the system prompt and the message text of each request."""

    def __init__(self) -> None:
        self.systems: list[str] = []
        self.inputs: list[str] = []
        self.url = ""

    def record(self, body: dict[str, Any]) -> None:
        system = body.get("system")
        if isinstance(system, list):
            system = " ".join(str(b.get("text", "")) for b in system)
        self.systems.append(str(system))
        texts: list[str] = []
        for message in body.get("messages", []):
            content = message.get("content")
            if isinstance(content, str):
                texts.append(content)
            elif isinstance(content, list):
                texts += [str(b.get("text", "")) for b in content if isinstance(b, dict)]
        self.inputs.append("\n".join(texts))


@pytest.fixture
def api() -> Iterator[Api]:
    fake = Api()

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args: object) -> None:
            return

        def do_POST(self) -> None:
            length = int(self.headers.get("content-length", 0))
            body = pydantic_core.from_json(self.rfile.read(length) or b"{}")
            if not self.path.startswith("/v1/messages") or "count_tokens" in self.path:
                self.send_response(200)
                self.send_header("content-type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"input_tokens": 1}')
                return
            fake.record(body)
            usage = {"input_tokens": 1, "output_tokens": 1}
            events: list[dict[str, Any]] = [
                {
                    "type": "message_start",
                    "message": {
                        "id": "msg_1",
                        "type": "message",
                        "role": "assistant",
                        "model": body.get("model", "m"),
                        "content": [],
                        "stop_reason": None,
                        "stop_sequence": None,
                        "usage": usage,
                    },
                },
                {"type": "content_block_start", "index": 0, "content_block": {"type": "text"}},
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "text_delta", "text": "ok"},
                },
                {"type": "content_block_stop", "index": 0},
                {
                    "type": "message_delta",
                    "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                    "usage": {"output_tokens": 1},
                },
                {"type": "message_stop"},
            ]
            events[1]["content_block"]["text"] = ""
            self.send_response(200)
            self.send_header("content-type", "text/event-stream")
            self.end_headers()
            for event in events:
                data = pydantic_core.to_json(event).decode()
                self.wfile.write(f"event: {event['type']}\ndata: {data}\n\n".encode())

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    fake.url = f"http://127.0.0.1:{server.server_address[1]}"
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield fake
    finally:
        server.shutdown()


class Transcripts:
    """Keeps entries per key, like core (ADR-0014)."""

    def __init__(self) -> None:
        self.stored: dict[tuple[str, str, str], list[TranscriptEntry]] = {}

    async def append_transcript(
        self, task_id: uuid.UUID, request: TranscriptAppend
    ) -> TranscriptAppended:
        key = request.key
        slot = self.stored.setdefault(
            (key.project_key, key.engine_session_id, key.subpath or ""), []
        )
        slot.extend(request.entries)
        return TranscriptAppended(stored=len(request.entries))

    async def load_transcript(self, task_id: uuid.UUID, query: TranscriptQuery) -> TranscriptLoaded:
        key = query.key
        found = self.stored.get((key.project_key, key.engine_session_id, key.subpath or ""), [])
        return TranscriptLoaded(entries=tuple(found))

    async def transcript_subpaths(
        self, task_id: uuid.UUID, query: SubpathQuery
    ) -> TranscriptSubpaths:
        return TranscriptSubpaths(subpaths=())


def _job(text: str, version: int, resume: str | None, message: str) -> EngineJob:
    return EngineJob(
        task_id=uuid.uuid4(),
        lease_id=uuid.uuid4(),
        attempt=1,
        session_id=uuid.uuid4(),
        engine_session_id=resume,
        definition=definition(),
        prompt=ResolvedPrompt(name="assistant", version=version, text=text, model="claude-t-1"),
        input=message,
    )


def _engine(tmp_path: Path, api: Api, transcripts: Transcripts) -> ClaudeEngine:
    (tmp_path / "work").mkdir(exist_ok=True)
    return ClaudeEngine(
        config=ClaudeEngineConfig(
            workdir=tmp_path / "work",
            scratch_dir=tmp_path / "scratch",
            tool_env={
                "ANTHROPIC_BASE_URL": api.url,
                "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
            },
        ),
        oauth_token=SecretStr("sk-ant-oat01-" + "t" * 40),
        transcripts=transcripts,
    )


async def test_a_new_prompt_reaches_an_old_thread(tmp_path: Path, api: Api) -> None:
    transcripts = Transcripts()
    engine = _engine(tmp_path, api, transcripts)
    first = await engine.run(_job(OLD, 4, None, "첫 메시지"))
    assert OLD in api.systems[-1]

    # Same prompt: the session is resumed as before.
    same = await engine.run(_job(OLD, 4, first.engine_session_id, "두 번째"))
    assert same.engine_session_id == first.engine_session_id
    assert OLD in api.systems[-1]

    # Promoted prompt: a new session with the new prompt, the old turns as text.
    after = await engine.run(_job(NEW, 6, first.engine_session_id, "승격 뒤 메시지"))
    assert NEW in api.systems[-1]
    assert OLD not in api.systems[-1]
    assert after.engine_session_id != first.engine_session_id
    assert PROMPT_CHANGED_HEADER in api.inputs[-1]
    assert "첫 메시지" in api.inputs[-1]
    assert "승격 뒤 메시지" in api.inputs[-1]

    # The thread then continues on the new session and the new prompt.
    later = await engine.run(_job(NEW, 6, after.engine_session_id, "그다음"))
    assert later.engine_session_id == after.engine_session_id
    assert NEW in api.systems[-1]


def test_snapshot_comparison() -> None:
    snapshot = {"type": "attachment", "attachment": {"type": "prompt_snapshot"}}
    old = {**snapshot, "attachment": {"type": "prompt_snapshot", "systemPrompt": [OLD]}}
    new = {**snapshot, "attachment": {"type": "prompt_snapshot", "systemPrompt": [NEW]}}
    assert session_prompt([old, new]) == (NEW,)
    assert prompt_changed([old], NEW)
    assert not prompt_changed([old], OLD)
    assert not prompt_changed([{"type": "user"}], NEW)  # nothing to compare: resume
    assert with_history("x", [], PROMPT_CHANGED_HEADER).startswith(PROMPT_CHANGED_EMPTY_HEADER)
