"""Enikk's memory from the owner's vault (ADR-0028): sync the index, hand out notes.

The vault repository is the source. Every interval core lists the vault's files,
fetches the notes whose blob changed and updates its index; deleted notes leave
the index. Each task receives enikk's preferences, facts and unexpired work.

Agents keep, update and forget notes through core, which writes them only under
enikk's folder in ``Private/`` with ``draft: true`` (both keep them off the public
blog), commits with ``[skip ci]``, refuses text that looks like a secret, and tells
the thread what it did.
"""

from __future__ import annotations

import logging
import re
import uuid  # noqa: TC003 - used at runtime in signatures
from datetime import date, datetime, timedelta
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Final, Protocol, Self

import yaml
from pydantic import model_validator

from enikk_contracts.common import ConnectorKind
from enikk_contracts.memory import MemoryMatch, MemoryMatches, MemoryWritten, NoteKind
from enikk_contracts.outbox import OutboundKind
from enikk_core_domain.errors import (
    DependencyUnavailableError,
    InvalidRequestError,
    NotFoundError,
    PermissionDeniedError,
)
from enikk_core_domain.memory import MAX_BODY, MemoryLayout, VaultNote, context_notes
from enikk_core_domain.model import OutboxMessage
from enikk_github.permissions import TokenPurpose
from enikk_kernel.models import FrozenModel

if TYPE_CHECKING:
    from enikk_contracts.memory import (
        ForgetRequest,
        MemoryNote,
        MemorySearch,
        RememberRequest,
        UpdateMemoryRequest,
    )
    from enikk_core.github import GitHubAppApi
    from enikk_core_domain.model import Session, Task
    from enikk_core_domain.ports import UnitOfWork, UnitOfWorkFactory
    from enikk_github.app import InstallationToken
    from enikk_github.vault import VaultFile
    from enikk_kernel.clock import Clock
    from enikk_kernel.ids import IdGenerator

_log = logging.getLogger(__name__)

_FRONTMATTER: Final = re.compile(r"\A---\r?\n(.*?)\r?\n---\r?\n?", re.S)
_HEADING: Final = re.compile(r"^#\s+(.+?)\s*$", re.M)
SECRET: Final = re.compile(
    r"(gh[pousr]_[A-Za-z0-9]{20,}|github_pat_|xox[abprs]-|xapp-|sk-ant-|sk-[A-Za-z0-9]{20,}"
    r"|AKIA[0-9A-Z]{16}|AIza[0-9A-Za-z_-]{30,}|-----BEGIN [A-Z ]*PRIVATE KEY)"
)
_UNSAFE_NAME: Final = re.compile(r'[\\/:*?"<>|#^\[\]\x00-\x1f]+')
FOLDER_OF: Final = {NoteKind.PREFERENCE: "선호", NoteKind.FACT: "사실", NoteKind.WORK: "진행 중"}
WORK_DAYS: Final = 14


class VaultApi(Protocol):
    async def files(self, token: InstallationToken, branch: str = "main") -> list[VaultFile]: ...
    async def text(self, token: InstallationToken, sha: str) -> str: ...


class VaultWriteApi(Protocol):
    async def put(
        self, token: InstallationToken, path: str, text: str, message: str, branch: str = "main"
    ) -> str: ...

    async def delete(
        self, token: InstallationToken, path: str, message: str, branch: str = "main"
    ) -> bool: ...


class MemoryPolicy(FrozenModel):
    repository: str
    branch: str = "main"
    layout: MemoryLayout = MemoryLayout()
    context_chars: int = 4_000

    @model_validator(mode="after")
    def _enikks_notes_stay_private(self) -> Self:
        private = f"{self.layout.notes_root}Private/"
        if not self.layout.enikk_dir.startswith(private) or not self.layout.enikk_dir.endswith("/"):
            raise ValueError(f"enikk's folder must be a folder under {private} (ADR-0028)")
        return self


def parse_note(path: str, sha: str, text: str, layout: MemoryLayout, now: datetime) -> VaultNote:
    """A vault note: optional YAML frontmatter, then Markdown."""
    meta: dict[str, object] = {}
    body = text
    match = _FRONTMATTER.match(text)
    if match:
        try:
            loaded = yaml.safe_load(match.group(1))
        except yaml.YAMLError:
            loaded = None
        if isinstance(loaded, dict):
            meta = {str(k): v for k, v in loaded.items()}
        body = text[match.end() :]
    heading = _HEADING.search(body)
    title = str(meta.get("title") or (heading.group(1) if heading else PurePosixPath(path).stem))
    return VaultNote(
        path=path,
        blob_sha=sha,
        title=title[:300],
        kind=layout.kind_of(path),
        body=body.strip()[:MAX_BODY],
        updated=_date(meta.get("updated")) or _date(meta.get("created")),
        expires=_date(meta.get("expires")),
        synced_at=now,
    )


def _date(value: object) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value.strip()[:10])
        except ValueError:
            return None
    return None


class MemoryService:
    def __init__(
        self,
        *,
        uow: UnitOfWorkFactory,
        app: GitHubAppApi,
        vault: VaultApi,
        policy: MemoryPolicy,
        clock: Clock,
        writer: VaultWriteApi | None = None,
        ids: IdGenerator | None = None,
    ) -> None:
        self._uow = uow
        self._app = app
        self._vault = vault
        self._policy = policy
        self._clock = clock
        self._writer = writer
        self._ids = ids

    @property
    def layout(self) -> MemoryLayout:
        return self._policy.layout

    async def sync_once(self) -> int:
        """Bring the index up to date with the vault. Returns how many notes changed."""
        token = await self._app.installation_token(self._policy.repository, TokenPurpose.WATCH)
        try:
            files = [
                f
                for f in await self._vault.files(token, self._policy.branch)
                if self.layout.readable(f.path)
            ]
            async with self._uow() as uow:
                indexed = await uow.vault.shas()
            changed = [f for f in files if indexed.get(f.path) != f.sha]
            now = self._clock.now()
            notes = []
            for file in changed:
                try:
                    text = await self._vault.text(token, file.sha)
                except DependencyUnavailableError as exc:
                    _log.warning("skipped vault note %s: %s", file.path, exc)
                    continue
                notes.append(parse_note(file.path, file.sha, text, self.layout, now))
        finally:
            await self._app.revoke(token)
        gone = sorted(set(indexed) - {f.path for f in files})
        if not notes and not gone:
            return 0
        async with self._uow() as uow:
            for note in notes:
                await uow.vault.upsert(note)
            await uow.vault.remove(gone)
            await uow.commit()
        _log.info("vault index: %d note(s) updated, %d removed", len(notes), len(gone))
        return len(notes) + len(gone)

    async def for_task(self) -> tuple[MemoryNote, ...]:
        """The notes every task receives: enikk's preferences, facts and current work."""
        async with self._uow() as uow:
            notes = list(await uow.vault.under(self.layout.enikk_dir))
        today = self._clock.now().date()
        return context_notes(notes, self.layout, today, self._policy.context_chars)

    # ------------------------------------------------------------ writing (ADR-0028 §4)

    async def remember(self, task_id: uuid.UUID, request: RememberRequest) -> MemoryWritten:
        task, session = await self._origin(task_id)
        _refuse_secrets(request.title, request.text)
        name = _file_name(request.title)
        path = f"{self.layout.enikk_dir}{FOLDER_OF[request.kind]}/{name}.md"
        today = self._clock.now().date()
        text = note_text(
            title=request.title,
            kind=request.kind,
            body=request.text,
            created=today,
            updated=today,
            expires=today + timedelta(days=WORK_DAYS) if request.kind is NoteKind.WORK else None,
            source=_source(session),
            by=task.agent,
        )
        await self._write(path, text, f"memory: remember {name}")
        await self._tell(task, session, f"기억했습니다: {request.title} ({self._short(path)})")
        return MemoryWritten(action="remembered", path=path)

    async def update(self, task_id: uuid.UUID, request: UpdateMemoryRequest) -> MemoryWritten:
        task, session = await self._origin(task_id)
        _refuse_secrets(request.text)
        current = await self._enikks_note(request.path)
        today = self._clock.now().date()
        kind = self.layout.kind_of(current.path)
        text = note_text(
            title=current.title,
            kind=kind,
            body=request.text,
            created=current.updated or today,
            updated=today,
            expires=today + timedelta(days=WORK_DAYS) if kind is NoteKind.WORK else None,
            source=_source(session),
            by=task.agent,
        )
        await self._write(current.path, text, f"memory: update {current.title}")
        await self._tell(
            task, session, f"기억을 고쳤습니다: {current.title} ({self._short(current.path)})"
        )
        return MemoryWritten(action="updated", path=current.path)

    async def forget(self, task_id: uuid.UUID, request: ForgetRequest) -> MemoryWritten:
        task, session = await self._origin(task_id)
        current = await self._enikks_note(request.path)
        token = await self._token()
        try:
            await self._require_writer().delete(
                token,
                current.path,
                f"memory: forget {current.title} [skip ci]",
                self._policy.branch,
            )
        finally:
            await self._app.revoke(token)
        async with self._uow() as uow:
            await uow.vault.remove([current.path])
            await uow.commit()
        await self._tell(
            task, session, f"잊었습니다: {current.title} ({self._short(current.path)})"
        )
        return MemoryWritten(action="forgotten", path=current.path)

    async def search(self, request: MemorySearch) -> MemoryMatches:
        words = [w for w in request.query.split() if w][:5]
        async with self._uow() as uow:
            notes = await uow.vault.search(words, request.limit)
        return MemoryMatches(
            matches=tuple(
                MemoryMatch(
                    path=n.path, title=n.title, kind=n.kind, snippet=_snippet(n.body, words)
                )
                for n in notes
            )
        )

    async def _origin(self, task_id: uuid.UUID) -> tuple[Task, Session]:
        """The task and its session; refuse unless the work began with the owner in Slack."""
        async with self._uow() as uow:
            task = await uow.tasks.get_for_update(task_id)
            session = None if task is None else await uow.sessions.get(task.session_id)
        if task is None or session is None:
            raise NotFoundError(f"task {task_id} not found")
        if session.conversation.connector is not ConnectorKind.SLACK:
            raise PermissionDeniedError("only work the owner started in Slack may change memory")
        return task, session

    async def _enikks_note(self, path: str) -> VaultNote:
        if not self.layout.is_enikks(path) or ".." in path.split("/"):
            raise PermissionDeniedError("enikk only changes its own notes under its folder")
        async with self._uow() as uow:
            found = await uow.vault.under(path)
        note = next((n for n in found if n.path == path), None)
        if note is None:
            raise NotFoundError(f"no memory note at {path}")
        return note

    async def _write(self, path: str, text: str, message: str) -> None:
        if not self.layout.is_enikks(path) or "\ndraft: true\n" not in text:
            raise PermissionDeniedError("refusing to write a note that could be published")
        token = await self._token()
        try:
            sha = await self._require_writer().put(
                token, path, text, f"{message} [skip ci]", self._policy.branch
            )
        finally:
            await self._app.revoke(token)
        note = parse_note(path, sha, text, self.layout, self._clock.now())
        async with self._uow() as uow:
            await uow.vault.upsert(note)
            await uow.commit()
        _log.info("memory note written: %s", path)

    async def _token(self) -> InstallationToken:
        return await self._app.installation_token(self._policy.repository, TokenPurpose.MEMORY)

    def _require_writer(self) -> VaultWriteApi:
        if self._writer is None:
            raise DependencyUnavailableError("memory writing is not configured")
        return self._writer

    async def _tell(self, task: Task, session: Session, text: str) -> None:
        if self._ids is None:
            return
        async with self._uow() as uow:
            await _post(uow, task, session, text, self._ids.new(), self._clock.now())
            await uow.commit()

    def _short(self, path: str) -> str:
        return path.removeprefix(self.layout.notes_root)


async def _post(
    uow: UnitOfWork, task: Task, session: Session, text: str, message_id: uuid.UUID, now: datetime
) -> None:
    await uow.outbox.add(
        OutboxMessage(
            id=message_id,
            task_id=task.id,
            source_event_id=task.source_event_id,
            conversation=session.outbox_conversation,
            kind=OutboundKind.REPLY,
            text=text,
            created_at=now,
        )
    )


def note_text(
    *,
    title: str,
    kind: NoteKind,
    body: str,
    created: date,
    updated: date,
    expires: date | None,
    source: str,
    by: str,
) -> str:
    """A memory note: frontmatter that keeps it off the blog, then the text."""
    meta: dict[str, object] = {
        "title": title,
        "kind": kind.value,
        "created": created.isoformat(),
        "updated": updated.isoformat(),
    }
    if expires is not None:
        meta["expires"] = expires.isoformat()
    meta.update({"source": source, "by": by, "draft": True})
    front = yaml.safe_dump(meta, allow_unicode=True, sort_keys=False)
    return f"---\n{front}---\n# {title}\n\n{body.strip()}\n"


def _refuse_secrets(*texts: str) -> None:
    if any(SECRET.search(t) for t in texts):
        raise InvalidRequestError("this looks like a secret; secrets are never kept in memory")


def _file_name(title: str) -> str:
    name = " ".join(_UNSAFE_NAME.sub(" ", title).split()).strip(". ")
    if not name:
        raise InvalidRequestError("the title has no usable characters for a file name")
    return name[:80]


def _source(session: Session) -> str:
    conversation = session.outbox_conversation
    return f"slack {conversation.channel} {conversation.thread}"


def _snippet(body: str, words: list[str]) -> str:
    lowered = body.lower()
    hits = [lowered.find(w.lower()) for w in words if lowered.find(w.lower()) >= 0]
    start = max(0, min(hits) - 150) if hits else 0
    text = " ".join(body[start : start + 500].split())
    return ("…" if start else "") + text[:590]
