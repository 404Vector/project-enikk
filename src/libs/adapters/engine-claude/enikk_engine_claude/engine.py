"""Claude Agent SDK engine. Built-in tools are only those the definition grants (I-14).

Every role may be granted built-in tools (ADR-0033). They run in a Landlock sandbox
(``sandbox``): the CLI gets the Claude token on a pipe, not in its environment, cannot
read the paths that hold the service's secrets, and runs each shell command in a
nested sandbox without secrets. Without Landlock, shared roles lose their built-in
tools; isolated roles keep them, as before ADR-0033.

A failed run is logged in detail (``failures``). A resumed session that fails before
the model answers, in a way a retry would not fix, is replaced by a new session that
gets the end of the old conversation as text (``resume``, ADR-0034).
"""

from __future__ import annotations

import contextlib
import logging
import os
import shutil
import sys
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, cast, get_args

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKError,
    EffortLevel,
    MirrorErrorMessage,
    ResultMessage,
    ServerToolUseBlock,
    SystemMessage,
    TextBlock,
    ToolUseBlock,
    project_key_for_directory,
    query,
)
from pydantic import Field, SecretStr

import enikk_kernel
from enikk_contracts.agents import Execution
from enikk_contracts.common import Engine
from enikk_engine_claude import sandbox
from enikk_engine_claude.audit import audit_hooks
from enikk_engine_claude.failures import RunTrace, failure_of_exception, failure_of_result
from enikk_engine_claude.platform_tools import SERVER as PLATFORM_SERVER
from enikk_engine_claude.platform_tools import granted_platform_tools, platform_server
from enikk_engine_claude.resume import PROMPT_CHANGED_HEADER, prompt_changed, with_history
from enikk_engine_claude.transcripts import CoreSessionStore
from enikk_kernel.models import FrozenModel
from enikk_runtime.errors import EngineError
from enikk_runtime.model import EngineResult, TokenUsage

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable, Iterator

    from claude_agent_sdk import Message, SessionKey

    from enikk_engine_claude.failures import Failure
    from enikk_engine_claude.transcripts import TranscriptApi
    from enikk_runtime.model import EngineJob
    from enikk_runtime.ports import Delegator, Diagnostician, MemoryKeeper, PromptProposer

    QueryFn = Callable[..., AsyncIterator[Message]]

MAX_ANSWER_CHARS: Final = 40_000
TRUNCATION_NOTE: Final = "\n\n…(truncated)"

SANDBOX_READ: Final = (
    "/usr",
    "/bin",
    "/sbin",
    "/lib",
    "/lib32",
    "/lib64",
    "/etc",
    "/opt",
    "/app",
    "/proc",
    "/sys",
    "/var/lib",
    "/var/cache",
)
"""Readable in the sandbox. ``/run`` (where Kubernetes mounts secrets) is not."""
SANDBOX_WRITE: Final = ("/dev",)
"""Writable besides the working directory and the scratch directory, which holds the
task's home and ``TMPDIR``. ``/tmp`` itself is not."""

_TOKEN_NAMES: Final = frozenset({"CLAUDE_CODE_OAUTH_TOKEN", sandbox.ENV_TOKEN, sandbox.TOKEN_FD})

_log = logging.getLogger(__name__)


class SandboxConfig(FrozenModel):
    """What the CLI and its tools may touch (ADR-0033)."""

    secrets: tuple[Path, ...] = ()
    """Must stay unreadable, e.g. the projected core token; checked at every start."""
    read: tuple[Path, ...] = tuple(Path(p) for p in SANDBOX_READ)
    write: tuple[Path, ...] = ()
    """Writable besides the working directory and the scratch directory."""


def _installed() -> Path:
    """Where this environment's console scripts are."""
    return Path(sys.executable).parent


def _bundled_cli() -> Path:
    import claude_agent_sdk

    return Path(claude_agent_sdk.__file__).parent / "_bundled" / "claude"


class ClaudeEngineConfig(FrozenModel):
    workdir: Path
    """Working directory of the Claude CLI. Must be the same path in every pod, because
    the transcript ``project_key`` is derived from it (ADR-0014)."""
    scratch_dir: Path
    """Parent of the per-task ``CLAUDE_CONFIG_DIR``; writable, discarded after each task."""
    client_app: str = Field(default="enikk-runtime-claude/0.1", pattern=r"^[\x21-\x7e]+$")
    tool_env: dict[str, str] = Field(default_factory=dict)
    """Extra environment for the CLI and the tools it runs (paths only, never secrets)."""
    sandbox: SandboxConfig = SandboxConfig()
    cli: Path = Field(default_factory=_bundled_cli)
    launcher: Path = Field(default_factory=lambda: _installed() / "enikk-claude-launch")
    shell_prefix: Path = Field(default_factory=lambda: _installed() / "enikk-sandbox-shell")


class ClaudeEngine:
    def __init__(
        self,
        *,
        config: ClaudeEngineConfig,
        oauth_token: SecretStr,
        transcripts: TranscriptApi,
        delegator: Delegator | None = None,
        memory: MemoryKeeper | None = None,
        diagnostics: Diagnostician | None = None,
        prompts: PromptProposer | None = None,
        query_fn: QueryFn = query,
        landlock: bool | None = None,
    ) -> None:
        self._config = config
        self._token = oauth_token
        self._transcripts = transcripts
        self._delegator = delegator
        self._memory = memory
        self._diagnostics = diagnostics
        self._prompts = prompts
        self._query = query_fn
        self._landlock = sandbox.available() if landlock is None else landlock
        if not self._landlock:
            _log.warning(
                "Landlock is unavailable: shared roles run without built-in tools (ADR-0033)"
            )

    @property
    def engine(self) -> Engine:
        return Engine.CLAUDE

    def accepts_model(self, model: str) -> bool:
        return model.startswith("claude-")

    async def run(self, job: EngineJob) -> EngineResult:
        if job.engine_session_id is not None:
            history = await self._history(job)
            if prompt_changed(history, job.prompt.text):
                # Resuming would keep the session's old system prompt (ADR-0032 §7).
                _log.info(
                    "task %s: prompt %s v%d differs from the one session %s started with; "
                    "starting a new session",
                    job.task_id,
                    job.prompt.name,
                    job.prompt.version,
                    job.engine_session_id,
                )
                text = with_history(job.input, history, PROMPT_CHANGED_HEADER)
                return await self._attempt(job, None, text)
        try:
            return await self._attempt(job, job.engine_session_id, job.input)
        except _ResumeRefusedError as refused:
            _log.warning(
                "task %s: could not resume session %s (%s); starting a new session",
                job.task_id,
                job.engine_session_id,
                refused.failure.reason,
            )
        history = await self._history(job)
        return await self._attempt(job, None, with_history(job.input, history))

    async def _attempt(self, job: EngineJob, resume: str | None, prompt: str) -> EngineResult:
        store = CoreSessionStore(self._transcripts, job.task_id, job.lease_id)
        self._config.scratch_dir.mkdir(parents=True, exist_ok=True)
        trace = RunTrace()
        with tempfile.TemporaryDirectory(prefix="claude-", dir=self._config.scratch_dir) as home:
            (Path(home) / "tmp").mkdir()
            options = self._options(job, store, Path(home), resume, trace)
            try:
                with _temp_root(Path(home)):
                    await self._consume(job, self._query(prompt=prompt, options=options), trace)
            except ClaudeSDKError as exc:
                failure = failure_of_exception(exc, trace)
                cause: BaseException | None = exc
            else:
                result = trace.result
                if result is not None and not result.is_error and result.subtype == "success":
                    return _finish(job, result, trace)
                failure = failure_of_result(result, trace)
                cause = None
            finally:
                if job.definition.execution is Execution.SHARED and options.tools:
                    _clear(self._config.workdir)
        _log.warning("task %s: Claude Code failed: %s", job.task_id, failure.detail)
        if resume is not None and not trace.answered and not failure.retryable:
            raise _ResumeRefusedError(failure) from cause
        raise EngineError(failure.reason, retryable=failure.retryable) from cause

    async def _history(self, job: EngineJob) -> list[dict[str, Any]]:
        """The saved transcript of the session that could not be resumed, if core has it."""
        if job.engine_session_id is None:
            return []
        store = CoreSessionStore(self._transcripts, job.task_id, job.lease_id)
        key: SessionKey = {
            "project_key": project_key_for_directory(str(self._config.workdir)),
            "session_id": job.engine_session_id,
        }
        try:
            entries = await store.load(key)
        except Exception as exc:
            _log.warning("task %s: could not load the old transcript: %s", job.task_id, exc)
            return []
        return [dict(e) for e in entries or []]

    def _options(
        self,
        job: EngineJob,
        store: CoreSessionStore,
        home: Path,
        resume: str | None,
        trace: RunTrace,
    ) -> ClaudeAgentOptions:
        effort = job.prompt.parameters.get("effort")
        unknown = sorted(set(job.prompt.parameters) - {"effort"})
        if unknown:
            _log.warning("prompt %s: ignoring unsupported config keys %s", job.prompt.name, unknown)
        builtin = self._builtin(job)
        server = platform_server(
            job, self._delegator, self._memory, self._diagnostics, self._prompts
        )
        platform = self._platform(job) if server is not None else []
        return ClaudeAgentOptions(
            tools=builtin,
            allowed_tools=[*builtin, *platform],
            mcp_servers={} if server is None else {PLATFORM_SERVER: server},
            setting_sources=[],
            strict_mcp_config=True,
            permission_mode="dontAsk",
            verbatim_prompts=True,
            system_prompt=job.prompt.text,
            model=job.prompt.model,
            effort=_effort(effort),
            max_turns=job.definition.limits.max_turns,
            resume=resume,
            cwd=str(self._config.workdir),
            session_store=store.as_sdk_store(),
            client_app=self._config.client_app,
            cli_path=self._config.launcher,
            hooks=audit_hooks(job),
            env={
                **{k: v for k, v in self._config.tool_env.items() if k not in _TOKEN_NAMES},
                **self._sandbox_env(home),
                sandbox.ENV_TOKEN: self._token.get_secret_value(),
                "ANTHROPIC_API_KEY": "",
                "CLAUDE_CONFIG_DIR": str(home),
                "HOME": str(home),
                "TMPDIR": str(home / "tmp"),
            },
            stderr=lambda line: _stderr(trace, line),
        )

    def _builtin(self, job: EngineJob) -> list[str]:
        """The granted built-in tools, or none for a shared role without Landlock."""
        builtin = list(job.definition.tools.builtin)
        if builtin and not self._landlock and job.definition.execution is Execution.SHARED:
            _log.error(
                "task %s: no Landlock, so role %s runs without its built-in tools",
                job.task_id,
                job.definition.name,
            )
            return []
        return builtin

    def _sandbox_env(self, home: Path) -> dict[str, str]:
        config = self._config
        code = [Path(sandbox.__file__).parents[1], Path(enikk_kernel.__file__).parents[1]]
        read = [
            *config.sandbox.read,
            Path(sys.prefix),
            Path(sys.base_prefix),
            config.cli.parent,
            *code,
        ]
        write = [
            config.workdir,
            config.scratch_dir,
            home,
            *(Path(p) for p in SANDBOX_WRITE),
            *config.sandbox.write,
        ]
        secrets = list(config.sandbox.secrets)
        return {
            sandbox.ENV_MODE: sandbox.LANDLOCK if self._landlock else sandbox.OFF,
            sandbox.ENV_READ: os.pathsep.join(str(p) for p in dict.fromkeys(read)),
            sandbox.ENV_WRITE: os.pathsep.join(str(p) for p in dict.fromkeys(write)),
            sandbox.ENV_SECRETS: os.pathsep.join(str(p) for p in secrets),
            sandbox.ENV_CLI: str(config.cli),
            "CLAUDE_CODE_SHELL_PREFIX": str(config.shell_prefix),
        }

    def _granted(self, job: EngineJob) -> tuple[str, ...]:
        return (*self._builtin(job), *self._platform(job))

    def _platform(self, job: EngineJob) -> list[str]:
        return granted_platform_tools(
            job,
            delegator=self._delegator is not None,
            memory=self._memory is not None,
            diagnostics=self._diagnostics is not None,
            prompts=self._prompts is not None,
        )

    async def _consume(
        self, job: EngineJob, stream: AsyncIterator[Message], trace: RunTrace
    ) -> None:
        try:
            async for message in stream:
                if isinstance(message, MirrorErrorMessage):
                    raise EngineError("the conversation could not be saved to core", retryable=True)
                if isinstance(message, SystemMessage) and message.subtype == "init":
                    _require_only_granted_tools(message.data, self._granted(job))
                elif isinstance(message, AssistantMessage):
                    _observe(trace, message)
                elif isinstance(message, ResultMessage):
                    trace.result = message
        finally:
            closer = getattr(stream, "aclose", None)
            if closer is not None:
                await closer()


class _ResumeRefusedError(Exception):
    """A resumed run failed before the model answered, and retrying would not help."""

    def __init__(self, failure: Failure) -> None:
        super().__init__(failure.reason)
        self.failure = failure


def _observe(trace: RunTrace, message: AssistantMessage) -> None:
    for block in message.content:
        if isinstance(block, ToolUseBlock | ServerToolUseBlock):
            trace.last_tool = block.name
    if message.parent_tool_use_id is not None:
        return
    if message.error is None:
        trace.answered = True
    trace.model = message.model or trace.model
    trace.texts.extend(b.text for b in message.content if isinstance(b, TextBlock))


def _stderr(trace: RunTrace, line: str) -> None:
    trace.stderr_line(line)
    _log.debug("claude: %s", line)


@contextlib.contextmanager
def _temp_root(path: Path) -> Iterator[None]:
    """Make ``path`` the process's temporary directory while the SDK runs: the SDK
    writes a resumed session there, and the sandboxed CLI must reach it."""
    previous = tempfile.tempdir
    tempfile.tempdir = str(path)
    try:
        yield
    finally:
        tempfile.tempdir = previous


def _clear(workdir: Path) -> None:
    """Empty a shared role's working directory, so one conversation's files do not
    reach the next."""
    if not workdir.is_dir():
        return
    for child in workdir.iterdir():
        if child.is_dir() and not child.is_symlink():
            shutil.rmtree(child, ignore_errors=True)
        else:
            child.unlink(missing_ok=True)


def _effort(value: object) -> EffortLevel | None:
    for level in get_args(EffortLevel):
        if value == level:
            return cast("EffortLevel", level)
    return None


def _require_only_granted_tools(init: dict[str, Any], granted: tuple[str, ...]) -> None:
    tools = init.get("tools") or []
    extra = sorted({str(t) for t in tools} - set(granted))
    if extra:
        raise EngineError(
            f"Claude Code exposed built-in tools {extra} the agent was not granted; "
            "refusing to run (I-14)",
            retryable=False,
        )


def _finish(job: EngineJob, result: ResultMessage, trace: RunTrace) -> EngineResult:
    answer = (result.result or "\n\n".join(trace.texts)).strip()
    if not answer:
        raise EngineError("Claude returned an empty answer", retryable=False)
    if len(answer) > MAX_ANSWER_CHARS:
        answer = answer[: MAX_ANSWER_CHARS - len(TRUNCATION_NOTE)] + TRUNCATION_NOTE
    _log.info(
        "task %s: Claude answered in %d turn(s), session %s",
        job.task_id,
        result.num_turns,
        result.session_id,
    )
    return EngineResult(
        text=answer,
        engine_session_id=result.session_id,
        model=trace.model or job.prompt.model,
        usage=_usage(result.usage),
    )


def _usage(raw: dict[str, Any] | None) -> TokenUsage | None:
    if not raw:
        return None

    def count(key: str) -> int:
        value = raw.get(key)
        return value if isinstance(value, int) and value >= 0 else 0

    return TokenUsage(
        input_tokens=count("input_tokens"),
        output_tokens=count("output_tokens"),
        cache_read_tokens=count("cache_read_input_tokens"),
        cache_creation_tokens=count("cache_creation_input_tokens"),
    )
