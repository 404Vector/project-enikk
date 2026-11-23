"""Claude Agent SDK engine. Built-in tools are only those the definition grants (I-14).

Shared agents get none (ADR-0017); only isolated agents may be granted any (ADR-0021).
"""

from __future__ import annotations

import logging
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
    SystemMessage,
    TextBlock,
    query,
)
from pydantic import Field, SecretStr

from enikk_contracts.common import Engine
from enikk_engine_claude.platform_tools import SERVER as PLATFORM_SERVER
from enikk_engine_claude.platform_tools import granted_platform_tools, platform_server
from enikk_engine_claude.transcripts import CoreSessionStore
from enikk_kernel.models import FrozenModel
from enikk_runtime.errors import EngineError
from enikk_runtime.model import EngineResult, TokenUsage

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable

    from claude_agent_sdk import Message

    from enikk_engine_claude.transcripts import TranscriptApi
    from enikk_runtime.model import EngineJob
    from enikk_runtime.ports import Delegator, Diagnostician, MemoryKeeper, PromptProposer

    QueryFn = Callable[..., AsyncIterator[Message]]

MAX_ANSWER_CHARS: Final = 40_000
TRUNCATION_NOTE: Final = "\n\n…(truncated)"
NOT_RETRYABLE_RESULTS: Final = frozenset({"error_max_turns", "error_max_budget_usd"})
NOT_RETRYABLE_STATUS: Final = frozenset({400, 401, 403, 404})

_log = logging.getLogger(__name__)


class ClaudeEngineConfig(FrozenModel):
    workdir: Path
    """Working directory of the Claude CLI. Must be the same path in every pod, because
    the transcript ``project_key`` is derived from it (ADR-0014)."""
    scratch_dir: Path
    """Parent of the per-task ``CLAUDE_CONFIG_DIR``; writable, discarded after each task."""
    client_app: str = Field(default="enikk-runtime-claude/0.1", pattern=r"^[\x21-\x7e]+$")
    tool_env: dict[str, str] = Field(default_factory=dict)
    """Extra environment for the CLI and the tools it runs (paths only, never secrets)."""


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
    ) -> None:
        self._config = config
        self._token = oauth_token
        self._transcripts = transcripts
        self._delegator = delegator
        self._memory = memory
        self._diagnostics = diagnostics
        self._prompts = prompts
        self._query = query_fn

    @property
    def engine(self) -> Engine:
        return Engine.CLAUDE

    def accepts_model(self, model: str) -> bool:
        return model.startswith("claude-")

    async def run(self, job: EngineJob) -> EngineResult:
        store = CoreSessionStore(self._transcripts, job.task_id, job.lease_id)
        self._config.scratch_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="claude-", dir=self._config.scratch_dir) as home:
            options = self._options(job, store, Path(home))
            try:
                return await self._consume(job, self._query(prompt=job.input, options=options))
            except ClaudeSDKError as exc:
                _log.warning("task %s: Claude Code failed: %s", job.task_id, type(exc).__name__)
                raise EngineError(
                    f"Claude Code process failed ({type(exc).__name__})", retryable=True
                ) from exc

    def _options(self, job: EngineJob, store: CoreSessionStore, home: Path) -> ClaudeAgentOptions:
        effort = job.prompt.parameters.get("effort")
        unknown = sorted(set(job.prompt.parameters) - {"effort"})
        if unknown:
            _log.warning("prompt %s: ignoring unsupported config keys %s", job.prompt.name, unknown)
        builtin = list(job.definition.tools.builtin)
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
            resume=job.engine_session_id,
            cwd=str(self._config.workdir),
            session_store=store.as_sdk_store(),
            client_app=self._config.client_app,
            env={
                **self._config.tool_env,
                "CLAUDE_CODE_OAUTH_TOKEN": self._token.get_secret_value(),
                "ANTHROPIC_API_KEY": "",
                "CLAUDE_CONFIG_DIR": str(home),
                "HOME": str(home),
            },
            stderr=lambda line: _log.debug("claude: %s", line),
        )

    def _granted(self, job: EngineJob) -> tuple[str, ...]:
        return (*job.definition.tools.builtin, *self._platform(job))

    def _platform(self, job: EngineJob) -> list[str]:
        return granted_platform_tools(
            job,
            delegator=self._delegator is not None,
            memory=self._memory is not None,
            diagnostics=self._diagnostics is not None,
            prompts=self._prompts is not None,
        )

    async def _consume(self, job: EngineJob, stream: AsyncIterator[Message]) -> EngineResult:
        texts: list[str] = []
        model: str | None = None
        result: ResultMessage | None = None
        try:
            async for message in stream:
                if isinstance(message, MirrorErrorMessage):
                    raise EngineError("the conversation could not be saved to core", retryable=True)
                if isinstance(message, SystemMessage) and message.subtype == "init":
                    _require_only_granted_tools(message.data, self._granted(job))
                elif isinstance(message, AssistantMessage) and message.parent_tool_use_id is None:
                    model = message.model or model
                    texts.extend(b.text for b in message.content if isinstance(b, TextBlock))
                elif isinstance(message, ResultMessage):
                    result = message
        finally:
            closer = getattr(stream, "aclose", None)
            if closer is not None:
                await closer()
        return _finish(job, result, texts, model)


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


def _finish(
    job: EngineJob, result: ResultMessage | None, texts: list[str], model: str | None
) -> EngineResult:
    if result is None:
        raise EngineError("Claude Code ended without a result", retryable=True)
    if result.is_error or result.subtype != "success":
        status = result.api_error_status
        retryable = (
            result.subtype not in NOT_RETRYABLE_RESULTS and status not in NOT_RETRYABLE_STATUS
        )
        detail = f", API status {status}" if status is not None else ""
        raise EngineError(f"Claude stopped: {result.subtype}{detail}", retryable=retryable)
    answer = (result.result or "\n\n".join(texts)).strip()
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
        model=model or job.prompt.model,
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
