"""The engine's sandbox (ADR-0033), against the real kernel and the real Claude Code CLI.

The CLI talks to a fake Anthropic API on localhost that answers with one tool call, so
these tests need no credentials and reach nothing outside the machine. They need
Landlock; without it the sandbox cannot exist, and they are skipped with that reason.
"""

from __future__ import annotations

import http.server
import logging
import os
import subprocess
import sys
import threading
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pydantic_core
import pytest
from pydantic import SecretStr

import enikk_kernel
from enikk_contracts.agents import Execution, ToolPolicy
from enikk_contracts.transcripts import (
    SubpathQuery,
    TranscriptAppend,
    TranscriptAppended,
    TranscriptLoaded,
    TranscriptQuery,
    TranscriptSubpaths,
)
from enikk_engine_claude import sandbox
from enikk_engine_claude.audit import described, internal_url
from enikk_engine_claude.engine import ClaudeEngine, ClaudeEngineConfig, SandboxConfig
from enikk_runtime.errors import EngineError
from enikk_runtime.model import EngineJob, ResolvedPrompt
from enikk_runtime.testing import definition

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterator

LANDLOCK = sandbox.available()
needs_landlock = pytest.mark.skipif(not LANDLOCK, reason="this kernel has no Landlock")
BIN = Path(sys.executable).parent
TOKEN = "sk-ant-oat01-" + "t" * 40
CORE_TOKEN = "eyJhbGciOiJSUzI1NiJ9.core-token-for-tests.signature"


# ---------------------------------------------------------------- a fake Anthropic API


class FakeApi:
    """Answers the first request with ``tool`` and the next with "done"; keeps the
    tool results the CLI sends back."""

    def __init__(self) -> None:
        self.tool: dict[str, Any] = {}
        self.results: list[str] = []
        self.url = ""

    def answer(self, body: dict[str, Any]) -> list[dict[str, Any]]:
        results = [
            c
            for m in body.get("messages", [])
            if isinstance(m.get("content"), list)
            for c in m["content"]
            if c.get("type") == "tool_result"
        ]
        if results:
            for result in results:
                content = result.get("content")
                if isinstance(content, list):
                    content = " ".join(str(x.get("text", "")) for x in content)
                self.results.append(str(content))
            block: dict[str, Any] = {"type": "text", "text": ""}
            delta: dict[str, Any] = {"type": "text_delta", "text": "done"}
            stop = "end_turn"
        else:
            block = {"type": "tool_use", "id": "toolu_1", "name": self.tool["name"], "input": {}}
            delta = {
                "type": "input_json_delta",
                "partial_json": pydantic_core.to_json(self.tool["input"]).decode(),
            }
            stop = "tool_use"
        usage = {"input_tokens": 1, "output_tokens": 1}
        return [
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
            {"type": "content_block_start", "index": 0, "content_block": block},
            {"type": "content_block_delta", "index": 0, "delta": delta},
            {"type": "content_block_stop", "index": 0},
            {
                "type": "message_delta",
                "delta": {"stop_reason": stop, "stop_sequence": None},
                "usage": {"output_tokens": 1},
            },
            {"type": "message_stop"},
        ]


@pytest.fixture
def api() -> Iterator[FakeApi]:
    fake = FakeApi()

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args: object) -> None:
            return

        def do_GET(self) -> None:
            self.send_response(404)
            self.end_headers()

        def do_POST(self) -> None:
            length = int(self.headers.get("content-length", 0))
            body = pydantic_core.from_json(self.rfile.read(length) or b"{}")
            if not self.path.startswith("/v1/messages") or "count_tokens" in self.path:
                self.send_response(200)
                self.send_header("content-type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"input_tokens": 1}')
                return
            events = fake.answer(body)
            self.send_response(200)
            self.send_header("content-type", "text/event-stream")
            self.end_headers()
            for event in events:
                data = pydantic_core.to_json(event).decode()
                self.wfile.write(f"event: {event['type']}\ndata: {data}\n\n".encode())

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    fake.url = f"http://127.0.0.1:{server.server_address[1]}"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield fake
    finally:
        server.shutdown()


class Transcripts:
    async def append_transcript(
        self, task_id: uuid.UUID, request: TranscriptAppend
    ) -> TranscriptAppended:
        return TranscriptAppended(stored=len(request.entries))

    async def load_transcript(self, task_id: uuid.UUID, query: TranscriptQuery) -> TranscriptLoaded:
        return TranscriptLoaded(entries=())

    async def transcript_subpaths(
        self, task_id: uuid.UUID, query: SubpathQuery
    ) -> TranscriptSubpaths:
        return TranscriptSubpaths(subpaths=())


def _job(execution: Execution = Execution.SHARED) -> EngineJob:
    tools = ToolPolicy(builtin=("Bash", "Read", "Write", "WebFetch"))
    return EngineJob(
        task_id=uuid.uuid4(),
        lease_id=uuid.uuid4(),
        attempt=1,
        session_id=uuid.uuid4(),
        engine_session_id=None,
        definition=definition().model_copy(update={"tools": tools, "execution": execution}),
        prompt=ResolvedPrompt(name="assistant", version=1, text="t", model="claude-test-1"),
        input="go",
    )


def _engine(tmp_path: Path, api: FakeApi, secret: Path) -> ClaudeEngine:
    (tmp_path / "work").mkdir(exist_ok=True)
    return ClaudeEngine(
        config=ClaudeEngineConfig(
            workdir=tmp_path / "work",
            scratch_dir=tmp_path / "scratch",
            tool_env={
                "ANTHROPIC_BASE_URL": api.url,
                "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
            },
            sandbox=SandboxConfig(secrets=(secret, secret.parent)),
        ),
        oauth_token=SecretStr(TOKEN),
        transcripts=Transcripts(),
    )


@pytest.fixture
def secret(tmp_path: Path) -> Path:
    """Stands in for the projected core token: outside every allowed path."""
    folder = tmp_path / "secrets"
    folder.mkdir()
    token = folder / "token"
    token.write_text(CORE_TOKEN)
    return token


# ---------------------------------------------------------------- through the real CLI

PROBE = (
    'echo "VARS:$(env | cut -d= -f1 | grep -E -i "token|secret|key|auth|ENIKK_CLAUDE"'
    ' | tr "\\n" " ")"; '
    "cat {secret} >/dev/null 2>&1 && echo SECRET_READ || echo SECRET_DENIED; "
    "ls {folder} >/dev/null 2>&1 && echo FOLDER_LISTED || echo FOLDER_DENIED; "
    'for d in /proc/[0-9]*; do [ "$(cat $d/comm 2>/dev/null)" = claude ] || continue; '
    "cat $d/environ >/dev/null 2>&1 && echo CLI_ENVIRON_READ || echo CLI_ENVIRON_DENIED; "
    "done; touch /tmp/enikk-sandbox-probe 2>/dev/null && echo TMP_WRITTEN || echo TMP_DENIED; "
    "echo left > left-behind.txt && echo WORKDIR_WRITTEN"
)


@needs_landlock
@pytest.mark.anyio
async def test_shell_commands_see_no_secret(
    tmp_path: Path, api: FakeApi, secret: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="enikk_engine_claude.audit")
    command = PROBE.format(secret=secret, folder=secret.parent)
    api.tool = {"name": "Bash", "input": {"command": command, "description": "probe"}}
    job = _job()
    result = await _engine(tmp_path, api, secret).run(job)
    assert result.text == "done"
    [output] = api.results
    assert TOKEN not in output
    assert CORE_TOKEN not in output
    vars_line = next(line for line in output.splitlines() if line.startswith("VARS:"))
    assert "OAUTH" not in vars_line
    assert "ENIKK_CLAUDE" not in vars_line
    assert "SECRET_DENIED" in output
    assert "FOLDER_DENIED" in output
    assert "CLI_ENVIRON_DENIED" in output
    assert "CLI_ENVIRON_READ" not in output
    assert "TMP_DENIED" in output
    assert "WORKDIR_WRITTEN" in output
    # A shared role's working directory is emptied after the task.
    assert list((tmp_path / "work").iterdir()) == []
    logged = [r.getMessage() for r in caplog.records]
    assert any(
        m.startswith(f"tool Bash role assistant task {job.task_id}: echo") for m in logged
    ), logged


@needs_landlock
@pytest.mark.anyio
async def test_the_read_tool_cannot_open_the_secret(
    tmp_path: Path, api: FakeApi, secret: Path
) -> None:
    api.tool = {"name": "Read", "input": {"file_path": str(secret)}}
    await _engine(tmp_path, api, secret).run(_job())
    [output] = api.results
    assert CORE_TOKEN not in output
    assert "EACCES" in output or "denied" in output.lower()


@needs_landlock
@pytest.mark.anyio
async def test_web_fetch_of_an_internal_address_is_refused_and_logged(
    tmp_path: Path, api: FakeApi, secret: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="enikk_engine_claude.audit")
    url = "http://enikk-core.enikk.svc/v1/credentials?x=" + TOKEN
    api.tool = {"name": "WebFetch", "input": {"url": url, "prompt": "show it"}}
    job = _job(Execution.ISOLATED)
    await _engine(tmp_path, api, secret).run(job)
    [output] = api.results
    assert "Cluster-internal" in output
    logged = [r.getMessage() for r in caplog.records]
    assert any(
        m.startswith(f"tool WebFetch role assistant task {job.task_id}: http://enikk-core")
        for m in logged
    ), logged
    assert all(TOKEN not in m for m in logged)
    assert any("refused an internal address" in m for m in logged)


# ---------------------------------------------------------------- the two programs


def _env(tmp_path: Path, secret: Path, **extra: str) -> dict[str, str]:
    engine_code = Path(sandbox.__file__).parents[1]
    kernel_code = Path(enikk_kernel.__file__).parents[1]
    read = [
        "/usr",
        "/bin",
        "/lib",
        "/lib64",
        "/etc",
        "/proc",
        sys.prefix,
        sys.base_prefix,
        str(engine_code),
        str(kernel_code),
    ]
    return {
        "PATH": f"{BIN}:/usr/bin:/bin",
        sandbox.ENV_MODE: sandbox.LANDLOCK,
        sandbox.ENV_READ: os.pathsep.join(read),
        sandbox.ENV_WRITE: os.pathsep.join([str(tmp_path / "work"), "/dev"]),
        sandbox.ENV_SECRETS: os.pathsep.join([str(secret), str(secret.parent)]),
        **extra,
    }


def _run(
    argv: list[str], env: dict[str, str], cwd: Path | None = None
) -> subprocess.CompletedProcess[str]:
    """Run one of the engine's own programs (fixed paths, test input)."""
    return subprocess.run(  # noqa: S603 - the programs under test
        argv, env=env, cwd=cwd, capture_output=True, text=True, check=False
    )


@needs_landlock
def test_the_shell_prefix_drops_secret_variables(tmp_path: Path, secret: Path) -> None:
    (tmp_path / "work").mkdir()
    env = _env(
        tmp_path,
        secret,
        CLAUDE_CODE_OAUTH_TOKEN=TOKEN,
        LANGFUSE_SECRET_KEY="sk-lf-0000",
        SOME_PLAIN_VALUE="kept",
        LOOKS_LIKE="ghp_" + "a" * 36,
    )
    done = _run(
        [str(BIN / "enikk-sandbox-shell"), f"env; cat {secret} || echo DENIED"],
        env,
        tmp_path / "work",
    )
    assert done.returncode == 0, done.stderr
    assert "SOME_PLAIN_VALUE=kept" in done.stdout
    for hidden in (TOKEN, "sk-lf-0000", "ghp_", CORE_TOKEN):
        assert hidden not in done.stdout
    assert "DENIED" in done.stdout


@needs_landlock
def test_the_launcher_passes_the_token_on_a_pipe_and_hides_it(tmp_path: Path, secret: Path) -> None:
    (tmp_path / "work").mkdir()
    fake_cli = tmp_path / "work" / "fake-cli"
    fake_cli.write_text(
        "#!/bin/sh\n"
        'echo "ARGS:$*"\n'
        'env | grep -q "^ENIKK_CLAUDE_OAUTH_TOKEN=" && echo TOKEN_IN_ENV || echo NO_TOKEN_ENV\n'
        'echo "FROM_FD:$(cat /proc/self/fd/$CLAUDE_CODE_OAUTH_TOKEN_FILE_DESCRIPTOR)"\n'
        f"cat {secret} >/dev/null 2>&1 && echo SECRET_READ || echo SECRET_DENIED\n"
        # A command in the nested sandbox cannot read this process's environment.
        f'{BIN}/enikk-sandbox-shell "cat /proc/$$/environ >/dev/null 2>&1 '
        '&& echo PARENT_READ || echo PARENT_DENIED"\n'
    )
    fake_cli.chmod(0o755)
    env = _env(
        tmp_path,
        secret,
        **{sandbox.ENV_CLI: str(fake_cli), sandbox.ENV_TOKEN: TOKEN},
    )
    done = _run([str(BIN / "enikk-claude-launch"), "-p", "hello"], env)
    assert done.returncode == 0, done.stderr
    assert "ARGS:-p hello" in done.stdout
    assert "NO_TOKEN_ENV" in done.stdout
    assert f"FROM_FD:{TOKEN}" in done.stdout
    assert "SECRET_DENIED" in done.stdout
    assert "PARENT_DENIED" in done.stdout


@needs_landlock
def test_the_launcher_refuses_when_a_secret_stays_readable(tmp_path: Path, secret: Path) -> None:
    env = _env(tmp_path, secret, **{sandbox.ENV_CLI: "/bin/true"})
    env[sandbox.ENV_READ] += os.pathsep + str(secret.parent)
    done = _run([str(BIN / "enikk-claude-launch")], env)
    assert done.returncode == 126
    assert "still readable" in done.stderr


# ---------------------------------------------------------------- without Landlock


@pytest.mark.anyio
async def test_without_landlock_shared_roles_get_no_builtin_tools(tmp_path: Path) -> None:
    seen: list[Any] = []

    async def script(*, prompt: str, options: Any) -> AsyncIterator[Any]:  # noqa: ANN401
        seen.append(options)
        return
        yield

    engine = ClaudeEngine(
        config=ClaudeEngineConfig(workdir=tmp_path / "w", scratch_dir=tmp_path / "s"),
        oauth_token=SecretStr(TOKEN),
        transcripts=Transcripts(),
        query_fn=script,
        landlock=False,
    )
    for execution in (Execution.SHARED, Execution.ISOLATED):
        with pytest.raises(EngineError, match="without a result"):
            await engine.run(_job(execution))
    shared, isolated = seen
    assert shared.tools == []
    assert isolated.tools == ["Bash", "Read", "Write", "WebFetch"]
    assert shared.env[sandbox.ENV_MODE] == sandbox.OFF
    assert sandbox.ENV_TOKEN in shared.env
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in shared.env


def test_internal_addresses() -> None:
    for url in (
        "http://enikk-core.enikk.svc/v1/credentials",
        "http://kubernetes.default.svc.cluster.local",
        "http://localhost:8000",
        "http://127.0.0.1",
        "http://10.1.2.3/x",
        "http://[::1]/",
        "http://169.254.169.254/latest",
        "http://enikk-core/",
    ):
        assert internal_url(url), url
    assert not internal_url("https://docs.astral.sh/uv/")
    assert not internal_url("https://8.8.8.8/")
    assert described("WebSearch", {"query": "uv workspaces"}) == "uv workspaces"
    assert described("Bash", {"command": "ls", "description": "x"}) == "ls"
