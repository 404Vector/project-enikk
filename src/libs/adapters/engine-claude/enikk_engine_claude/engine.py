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
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, cast, get_args

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
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
from enikk_engine_claude import github_access, kube_access, sandbox
from enikk_engine_claude.audit import audit_hooks
from enikk_engine_claude.current import CurrentJob
from enikk_engine_claude.failures import RunTrace, failure_of_exception, failure_of_result
from enikk_engine_claude.live import LivePolicy, LivePool, LiveSession
from enikk_engine_claude.mcp_gateway import ENV_TOKEN as MCP_ENV_TOKEN
from enikk_engine_claude.mcp_gateway import gateway_tools
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
    from enikk_runtime.model import EngineJob, SkillUse
    from enikk_runtime.ports import (
        Diagnostician,
        MemoryKeeper,
        PromptProposer,
        SkillLibrary,
    )

    QueryFn = Callable[..., AsyncIterator[Message]]
    ClientFn = Callable[[ClaudeAgentOptions], ClaudeSDKClient]

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

_KNOWN_CONFIG: Final = frozenset({"effort", "description", "requires"})
"""Prompt config keys besides the model; ``description`` and ``requires`` are for skills
(ADR-0037 §2) and do not change the run."""
_TOKEN_NAMES: Final = frozenset(
    {"CLAUDE_CODE_OAUTH_TOKEN", sandbox.ENV_TOKEN, sandbox.TOKEN_FD, MCP_ENV_TOKEN}
)

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
    """Working directory of the Claude CLI; a shared role runs in a per-task directory
    under it. Must be the same path in every pod, because the transcript ``project_key``
    is derived from it, not from the per-task directory (ADR-0014, ADR-0037 3a)."""
    scratch_dir: Path
    """Parent of the per-task ``CLAUDE_CONFIG_DIR``; writable, discarded after each task."""
    client_app: str = Field(default="enikk-runtime-claude/0.1", pattern=r"^[\x21-\x7e]+$")
    tool_env: dict[str, str] = Field(default_factory=dict)
    """Extra environment for the CLI and the tools it runs (paths only, never secrets)."""
    sandbox: SandboxConfig = SandboxConfig()
    cli: Path = Field(default_factory=_bundled_cli)
    launcher: Path = Field(default_factory=lambda: _installed() / "enikk-claude-launch")
    shell_prefix: Path = Field(default_factory=lambda: _installed() / "enikk-sandbox-shell")
    mcp_gateway: str | None = None
    """agentgateway's base URL; ``None`` attaches no MCP server (ADR-0039 §2)."""
    live: LivePolicy | None = None
    """Keep a thread's Claude process between its tasks (ADR-0041 §3, 4b); ``None`` starts
    one per task."""
    kube: kube_access.KubeAccess | None = None
    """The runtime's read-only Kubernetes credentials, copied into each run's home
    (ADR-0041 3c); ``None`` gives the shell no cluster access."""


class ClaudeEngine:
    def __init__(
        self,
        *,
        config: ClaudeEngineConfig,
        oauth_token: SecretStr,
        transcripts: TranscriptApi,
        memory: MemoryKeeper | None = None,
        diagnostics: Diagnostician | None = None,
        prompts: PromptProposer | None = None,
        skills: SkillLibrary | None = None,
        query_fn: QueryFn = query,
        client_fn: ClientFn = ClaudeSDKClient,
        landlock: bool | None = None,
    ) -> None:
        self._config = config
        self._token = oauth_token
        self._transcripts = transcripts
        self._memory = memory
        self._diagnostics = diagnostics
        self._prompts = prompts
        self._skills = skills
        self._query = query_fn
        self._client = client_fn
        self._live = None if config.live is None else LivePool(config.live)
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
        if self._live is not None and job.definition.execution is Execution.SHARED:
            return await self._run_live(self._live, job)
        return await self._run_once(job)

    async def aclose(self) -> None:
        if self._live is not None:
            await self._live.close_all()

    async def _run_live(self, pool: LivePool, job: EngineJob) -> EngineResult:
        """Run ``job`` in its thread's live Claude process, starting one if there is none.
        Anything unusual falls back to a process for this task alone (``_run_once``)."""
        pool.sweep(_now())
        key = (job.prompt.name, job.prompt.version, job.prompt.model)
        live = pool.take(job.session_id, key, _now())
        opened = live is None
        if live is None:
            if job.engine_session_id is not None and prompt_changed(
                await self._history(job), job.prompt.text
            ):
                return await self._run_once(job)
            try:
                live = await self._open_live(job, key)
            except Exception:
                _log.exception("task %s: could not start a live session", job.task_id)
                return await self._run_once(job)
            pool.add(live)
        else:
            live.current.job = job
            self._credentials(live.home, job)
        trace = RunTrace()
        live.loaded.clear()
        trace.skills = live.loaded
        live.current.trace = trace
        try:
            with _temp_root(self._config.scratch_dir):
                await live.client.query(job.input)
                await self._consume(job, live.client.receive_response(), trace)
        except ClaudeSDKError as exc:
            pool.drop(live, "the run failed")
            failure = failure_of_exception(exc, trace)
        except BaseException:
            pool.drop(live, "the run was stopped")
            raise
        else:
            result = trace.result
            if result is not None and not result.is_error and result.subtype == "success":
                pool.release(live, _now())
                trace.skills = list(live.loaded)
                return _finish(job, result, trace)
            pool.drop(live, "the run failed")
            failure = failure_of_result(result, trace)
        _log.warning("task %s: Claude Code failed: %s", job.task_id, failure.detail)
        if opened and not trace.answered:
            # A new live process that never answered: run this task the old way.
            return await self._run_once(job)
        raise EngineError(failure.reason, retryable=failure.retryable)

    async def _open_live(self, job: EngineJob, key: tuple[str, int, str]) -> LiveSession:
        # Each process gets its own directories: the previous process of the thread may
        # still be closing in the background and removes only its own.
        name = f"{job.session_id}-{uuid.uuid4().hex[:8]}"
        home = self._config.scratch_dir / "live" / name
        cwd = self._config.workdir / f"live-{name}"
        (home / "tmp").mkdir(parents=True)
        cwd.mkdir(parents=True)
        self._credentials(home, job)
        current = CurrentJob(job, RunTrace())
        loaded: list[SkillUse] = []
        options = self._options(
            current, self._store(current), home, cwd, job.engine_session_id, loaded
        )
        client = self._client(options)
        with _temp_root(self._config.scratch_dir):
            await client.connect()
        _log.info("session %s: started a live Claude process", job.session_id)
        return LiveSession(
            session_id=job.session_id,
            client=client,
            current=current,
            home=home,
            cwd=cwd,
            prompt=key,
            loaded=loaded,
            now=_now(),
        )

    def _credentials(self, home: Path, job: EngineJob) -> None:
        """The task's GitHub token and the current Kubernetes token, in the run's home."""
        if job.github_token is not None:
            github_access.install(home, job.github_token)
        if self._config.kube is not None and not kube_access.install(home, self._config.kube):
            _log.warning("task %s: Kubernetes credentials are not mounted", job.task_id)

    async def _run_once(self, job: EngineJob) -> EngineResult:
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
        trace = RunTrace()
        current = CurrentJob(job, trace)
        store = self._store(current)
        self._config.scratch_dir.mkdir(parents=True, exist_ok=True)
        cwd = self._cwd(job)
        with tempfile.TemporaryDirectory(prefix="claude-", dir=self._config.scratch_dir) as home:
            (Path(home) / "tmp").mkdir()
            self._credentials(Path(home), job)
            cwd.mkdir(parents=True, exist_ok=True)
            options = self._options(current, store, Path(home), cwd, resume, trace.skills)
            try:
                with _temp_root(self._config.scratch_dir):
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
                if cwd != self._config.workdir:
                    shutil.rmtree(cwd, ignore_errors=True)
        _log.warning("task %s: Claude Code failed: %s", job.task_id, failure.detail)
        if resume is not None and not trace.answered and not failure.retryable:
            raise _ResumeRefusedError(failure) from cause
        raise EngineError(failure.reason, retryable=failure.retryable) from cause

    async def _history(self, job: EngineJob) -> list[dict[str, Any]]:
        """The saved transcript of the session that could not be resumed, if core has it."""
        if job.engine_session_id is None:
            return []
        store = self._store(CurrentJob(job))
        key: SessionKey = {
            "project_key": self._project_key(),
            "session_id": job.engine_session_id,
        }
        try:
            entries = await store.load(key)
        except Exception as exc:
            _log.warning("task %s: could not load the old transcript: %s", job.task_id, exc)
            return []
        return [dict(e) for e in entries or []]

    def _project_key(self) -> str:
        """The transcript key of every session: from ``workdir``, whatever the cwd."""
        return project_key_for_directory(str(self._config.workdir))

    def _store(self, current: CurrentJob) -> CoreSessionStore:
        return CoreSessionStore(self._transcripts, current, project_key=self._project_key())

    def _cwd(self, job: EngineJob) -> Path:
        """A shared role runs in its own directory under ``workdir``, so tasks running
        at the same time do not share files; an isolated role runs in ``workdir``."""
        if job.definition.execution is Execution.SHARED:
            return self._config.workdir / str(job.task_id)
        return self._config.workdir

    def _options(
        self,
        current: CurrentJob,
        store: CoreSessionStore,
        home: Path,
        cwd: Path,
        resume: str | None,
        loaded: list[SkillUse],
    ) -> ClaudeAgentOptions:
        job = current.job
        effort = job.prompt.parameters.get("effort")
        unknown = sorted(set(job.prompt.parameters) - _KNOWN_CONFIG)
        if unknown:
            _log.warning("prompt %s: ignoring unsupported config keys %s", job.prompt.name, unknown)
        builtin = self._builtin(job)
        server = platform_server(
            current,
            self._memory,
            self._diagnostics,
            self._prompts,
            self._skills,
            loaded,
        )
        platform = self._platform(job) if server is not None else []
        mcp = gateway_tools(job, self._config.mcp_gateway, reserved=PLATFORM_SERVER)
        servers: dict[str, Any] = dict(mcp.servers)
        if server is not None:
            servers[PLATFORM_SERVER] = server
        return ClaudeAgentOptions(
            tools=builtin,
            allowed_tools=[*builtin, *platform, *mcp.allowed],
            mcp_servers=servers,
            setting_sources=[],
            strict_mcp_config=True,
            permission_mode="dontAsk",
            verbatim_prompts=True,
            system_prompt=job.prompt.text,
            model=job.prompt.model,
            effort=_effort(effort),
            max_turns=job.definition.limits.max_turns,
            resume=resume,
            cwd=str(cwd),
            session_store=store.as_sdk_store(),
            client_app=self._config.client_app,
            cli_path=self._config.launcher,
            hooks=audit_hooks(current),
            env={
                **{k: v for k, v in self._config.tool_env.items() if k not in _TOKEN_NAMES},
                **self._sandbox_env(home, cwd, job),
                **mcp.env,
                sandbox.ENV_TOKEN: self._token.get_secret_value(),
                "ANTHROPIC_API_KEY": "",
                "CLAUDE_CONFIG_DIR": str(home),
                "HOME": str(home),
                "TMPDIR": str(home / "tmp"),
            },
            stderr=lambda line: _stderr(current.trace, line),
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

    def _sandbox_env(self, home: Path, cwd: Path, job: EngineJob) -> dict[str, str]:
        config = self._config
        workspace = job.workspace
        code = [Path(sandbox.__file__).parents[1], Path(enikk_kernel.__file__).parents[1]]
        read = [
            *config.sandbox.read,
            Path(sys.prefix),
            Path(sys.base_prefix),
            config.cli.parent,
            *code,
            *(workspace.read_paths if workspace else ()),
        ]
        # Only this run's home under ``scratch_dir``; the launcher adds the SDK's resume
        # directory when there is one (ADR-0037 3b).
        write = [
            cwd,
            home,
            *(Path(p) for p in SANDBOX_WRITE),
            *config.sandbox.write,
            # Only this session's worktree and tmp on the shared volume (ADR-0037 §7).
            *(workspace.write_paths if workspace else ()),
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
        mcp = gateway_tools(job, self._config.mcp_gateway, reserved=PLATFORM_SERVER)
        return (*self._builtin(job), *self._platform(job), *mcp.allowed)

    def _platform(self, job: EngineJob) -> list[str]:
        return granted_platform_tools(
            job,
            memory=self._memory is not None,
            diagnostics=self._diagnostics is not None,
            prompts=self._prompts is not None,
            skills=self._skills is not None,
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


def _stderr(trace: RunTrace | None, line: str) -> None:
    if trace is not None:
        trace.stderr_line(line)
    _log.debug("claude: %s", line)


_temp_users = 0
_temp_previous: str | None = None


@contextlib.contextmanager
def _temp_root(path: Path) -> Iterator[None]:
    """Make ``path`` the process's temporary directory while any run is going: the SDK
    writes a resumed session there, and the sandboxed CLI must reach it. Runs at the
    same time share it; the last one to end restores the previous value."""
    global _temp_users, _temp_previous
    if _temp_users == 0:
        _temp_previous = tempfile.tempdir
    _temp_users += 1
    tempfile.tempdir = str(path)
    try:
        yield
    finally:
        _temp_users -= 1
        if _temp_users == 0:
            tempfile.tempdir = _temp_previous


def _now() -> datetime:
    return datetime.now(UTC)


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
        skills=tuple(trace.skills),
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
