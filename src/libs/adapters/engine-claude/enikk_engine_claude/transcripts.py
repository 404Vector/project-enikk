"""Agent SDK ``SessionStore`` that keeps transcripts in core (ADR-0014)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Final, Protocol, cast

from pydantic import TypeAdapter

from enikk_contracts.transcripts import (
    MAX_ENTRIES_PER_APPEND,
    SubpathQuery,
    TranscriptAppend,
    TranscriptEntry,
    TranscriptKey,
    TranscriptQuery,
)

if TYPE_CHECKING:
    import uuid

    from claude_agent_sdk import (
        SessionKey,
        SessionListSubkeysKey,
        SessionStore,
        SessionStoreEntry,
    )

    from enikk_contracts.transcripts import (
        TranscriptAppended,
        TranscriptLoaded,
        TranscriptSubpaths,
    )
    from enikk_engine_claude.current import CurrentJob

_ENTRIES: Final = TypeAdapter(tuple[TranscriptEntry, ...])


class TranscriptApi(Protocol):
    """The transcript half of the core API. ``CoreClient`` implements it."""

    async def append_transcript(
        self, task_id: uuid.UUID, request: TranscriptAppend
    ) -> TranscriptAppended: ...

    async def load_transcript(
        self, task_id: uuid.UUID, query: TranscriptQuery
    ) -> TranscriptLoaded: ...

    async def transcript_subpaths(
        self, task_id: uuid.UUID, query: SubpathQuery
    ) -> TranscriptSubpaths: ...


class CoreSessionStore:
    """Scoped to one leased task: core accepts calls only while the lease is active.

    Implements ``append``, ``load`` and ``list_subkeys``. The SDK probes for its other,
    optional methods at runtime and treats missing ones as absent.
    """

    def __init__(
        self,
        api: TranscriptApi,
        current: CurrentJob,
        *,
        project_key: str | None = None,
    ) -> None:
        self._api = api
        self._current = current
        """The task and lease are read at each call (ADR-0041 §3)."""
        self._project_key = project_key
        """When set, replaces the ``project_key`` of every key the SDK passes in."""

    def as_sdk_store(self) -> SessionStore:
        """The SDK's Protocol also declares the optional methods, so it needs a cast."""
        return cast("SessionStore", self)

    async def append(self, key: SessionKey, entries: list[SessionStoreEntry]) -> None:
        if not entries:
            return
        validated = _ENTRIES.validate_python(entries)
        transcript = self._key(key)
        for start in range(0, len(validated), MAX_ENTRIES_PER_APPEND):
            await self._api.append_transcript(
                self._current.job.task_id,
                TranscriptAppend(
                    lease_id=self._current.job.lease_id,
                    key=transcript,
                    entries=validated[start : start + MAX_ENTRIES_PER_APPEND],
                ),
            )

    async def load(self, key: SessionKey) -> list[SessionStoreEntry] | None:
        loaded = await self._api.load_transcript(
            self._current.job.task_id,
            TranscriptQuery(lease_id=self._current.job.lease_id, key=self._key(key)),
        )
        if not loaded.entries:
            return None
        # Entries round-trip unchanged; their SDK type is a structural TypedDict.
        return cast("list[SessionStoreEntry]", [dict(entry) for entry in loaded.entries])

    async def list_subkeys(self, key: SessionListSubkeysKey) -> list[str]:
        found = await self._api.transcript_subpaths(
            self._current.job.task_id,
            SubpathQuery(
                lease_id=self._current.job.lease_id,
                project_key=self._project_key or key["project_key"],
                engine_session_id=key["session_id"],
            ),
        )
        return list(found.subpaths)

    def _key(self, key: SessionKey) -> TranscriptKey:
        transcript = _transcript_key(key)
        if self._project_key is None:
            return transcript
        return transcript.model_copy(update={"project_key": self._project_key})


def _transcript_key(key: SessionKey) -> TranscriptKey:
    return TranscriptKey(
        project_key=key["project_key"],
        engine_session_id=key["session_id"],
        subpath=key.get("subpath") or None,
    )
