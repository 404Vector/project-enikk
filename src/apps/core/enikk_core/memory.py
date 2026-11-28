"""Enikk's memory from the owner's vault (ADR-0028): sync the index, hand out notes.

The vault repository is the source. Every interval core lists the vault's files,
fetches the notes whose blob changed and updates its index; deleted notes leave
the index. Each task receives enikk's preferences, facts and unexpired work.

Agents keep, update and forget notes through core, which writes them only under
enikk's folder in ``Private/`` with ``draft: true`` (both keep them off the public
blog), commits with ``[skip ci]``, refuses text that looks like a secret, and tells
the thread what it did.

Agents also read any note of the vault and write the owner's public notes, the
blog (ADR-0049): only from work the owner started, outside the folders the blog
leaves out. A new note is always a draft, and a write never publishes or
unpublishes a note; that stays with the owner. The same tool writes enikk's
reference notes under its folder's ``자료/`` (ADR-0050), always as drafts.
"""

from __future__ import annotations

import logging
import re
import uuid  # noqa: TC003 - used at runtime in signatures
from datetime import date, datetime, timedelta
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Final, Literal, Protocol, Self

import yaml
from pydantic import model_validator

from enikk_contracts.agents import PlatformTool
from enikk_contracts.common import ConnectorKind
from enikk_contracts.memory import (
    MemoryMatch,
    MemoryMatches,
    MemoryWritten,
    NoteChangeAction,
    NoteChangeCreated,
    NoteChangeLinked,
    NoteChangeRequested,
    NoteChangeResult,
    NoteChangeStatus,
    NoteContent,
    NoteKind,
    NoteWritten,
)
from enikk_contracts.outbox import OutboundKind
from enikk_core.wakeups import NOTE_CHANGE_STATES, note_change_event_text
from enikk_core_domain.errors import (
    DependencyUnavailableError,
    InvalidRequestError,
    NotFoundError,
    PermissionDeniedError,
)
from enikk_core_domain.memory import MAX_BODY, MemoryLayout, NoteChange, VaultNote, context_notes
from enikk_core_domain.model import Notice, OutboxMessage
from enikk_core_domain.redaction import looks_secret
from enikk_github.permissions import TokenPurpose
from enikk_kernel.models import FrozenModel

if TYPE_CHECKING:
    from enikk_contracts.memory import (
        ForgetRequest,
        MemoryNote,
        MemorySearch,
        NoteChangeDecision,
        NoteChangePosted,
        NoteChangeRequest,
        NoteRead,
        NoteWrite,
        RememberRequest,
        UpdateMemoryRequest,
    )
    from enikk_core.github import GitHubAppApi
    from enikk_core.wakeups import SessionWaker
    from enikk_core_domain.model import Session, Task
    from enikk_core_domain.ports import AgentCatalog, UnitOfWork, UnitOfWorkFactory
    from enikk_github.app import InstallationToken
    from enikk_github.vault import VaultFile
    from enikk_kernel.clock import Clock
    from enikk_kernel.ids import IdGenerator

_log = logging.getLogger(__name__)

_FRONTMATTER: Final = re.compile(r"\A---\r?\n(.*?)\r?\n---\r?\n?", re.S)
_HEADING: Final = re.compile(r"^#\s+(.+?)\s*$", re.M)
_UNSAFE_NAME: Final = re.compile(r'[\\/:*?"<>|#^\[\]\x00-\x1f]+')
FOLDER_OF: Final = {NoteKind.PREFERENCE: "선호", NoteKind.FACT: "사실", NoteKind.WORK: "진행 중"}
WORK_DAYS: Final = 14
EXCERPT_CHARS: Final = 1_500
CHANGE_NAMES: Final = {
    NoteChangeAction.PUBLISH: "발행",
    NoteChangeAction.UNPUBLISH: "내리기",
    NoteChangeAction.DELETE: "삭제",
    NoteChangeAction.MOVE: "이동",
}
NOTE_CHARS: Final = 100_000
"""Characters of a note handed to an agent that reads it."""
CONVERSATION_FOLDER: Final = "대화"
CONVERSATIONS_BY: Final = "enikk"
"""The author of the daily conversation notes: enikk, whatever made the summary."""


class VaultApi(Protocol):
    async def files(self, token: InstallationToken, branch: str = "main") -> list[VaultFile]: ...
    async def text(self, token: InstallationToken, sha: str) -> str: ...


class VaultWriteApi(Protocol):
    async def current_sha(self, token: InstallationToken, path: str, branch: str) -> str | None: ...

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


def is_draft(text: str) -> bool:
    """Whether the blog leaves the note out: ``draft: true`` in its frontmatter."""
    match = _FRONTMATTER.match(text)
    if not match:
        return False
    try:
        meta = yaml.safe_load(match.group(1))
    except yaml.YAMLError:
        return False
    if not isinstance(meta, dict):
        return False
    value = meta.get("draft")
    return value is True or (isinstance(value, str) and value.strip().lower() == "true")


def set_draft(text: str, *, draft: bool) -> str:
    """``text`` with its frontmatter's ``draft`` set: ``draft: true``, or no draft line."""
    match = _FRONTMATTER.match(text)
    if match is None:
        return f"---\ndraft: true\n---\n{text}" if draft else text
    lines = [line for line in match.group(1).splitlines() if not re.match(r"^draft\s*:", line)]
    if draft:
        lines.append("draft: true")
    front = "\n".join(lines)
    return f"---\n{front}\n---\n{text[match.end() :]}" if front else text[match.end() :]


def _quiet(message: str, quiet: bool) -> str:
    """Mark a commit so the vault's blog build skips it when nothing published changes."""
    return f"{message} [skip ci]" if quiet else message


def _excerpt(text: str) -> str:
    return text if len(text) <= EXCERPT_CHARS else text[: EXCERPT_CHARS - 1] + "…"


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
        agents: AgentCatalog | None = None,
        owner: str | None = None,
        waker: SessionWaker | None = None,
    ) -> None:
        self._uow = uow
        self._agents = agents
        self._owner = owner
        self._waker = waker
        self._app = app
        self._vault = vault
        self._policy = policy
        self._clock = clock
        self._writer = writer
        self._ids = ids

    @property
    def layout(self) -> MemoryLayout:
        return self._policy.layout

    def _by(self, task: Task) -> str:
        """The agent that wrote a note: its current name, also for a former name."""
        definition = None if self._agents is None else self._agents.get(task.agent)
        return task.agent if definition is None else definition.name

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
            by=self._by(task),
        )
        await self._write(path, text, f"memory: remember {name}")
        await self._tell(
            task,
            session,
            OutboundKind.REMEMBERED,
            f"기억했습니다: {request.title} ({self._short(path)})",
        )
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
            by=self._by(task),
        )
        await self._write(current.path, text, f"memory: update {current.title}")
        await self._tell(
            task,
            session,
            OutboundKind.MEMORY_UPDATED,
            f"기억을 고쳤습니다: {current.title} ({self._short(current.path)})",
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
            task,
            session,
            OutboundKind.FORGOTTEN,
            f"잊었습니다: {current.title} ({self._short(current.path)})",
        )
        return MemoryWritten(action="forgotten", path=current.path)

    async def write_conversations(self, day: date, title: str, body: str, sources: int) -> str:
        """Create or replace the conversation summary of ``day`` (ADR-0035). Returns its path."""
        path = f"{self.layout.enikk_dir}{CONVERSATION_FOLDER}/{day.isoformat()}.md"
        _refuse_secrets(title, body)
        today = self._clock.now().date()
        text = note_text(
            title=title,
            kind=NoteKind.CONVERSATION,
            body=body,
            created=today,
            updated=today,
            expires=None,
            source=f"enikk 대화 기록 ({sources}개 스레드)",
            by=CONVERSATIONS_BY,
        )
        await self._write(path, text, f"memory: conversations of {day.isoformat()}")
        return path

    async def write_scheduled(
        self, folder: str, stem: str, title: str, body: str, kind: NoteKind, source: str
    ) -> str:
        """Create or replace a scheduled run's note under enikk's folder (ADR-0039 S7,
        output ``vault-note``). Returns its path."""
        path = f"{self.layout.enikk_dir}{folder}/{stem}.md"
        _refuse_secrets(title, body)
        today = self._clock.now().date()
        text = note_text(
            title=title,
            kind=kind,
            body=body,
            created=today,
            updated=today,
            expires=None,
            source=source,
            by=CONVERSATIONS_BY,
        )
        await self._write(path, text, f"memory: scheduled note {stem}")
        return path

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

    # ------------------------------------------------------------ the blog (ADR-0049)

    async def read_note(self, request: NoteRead) -> NoteContent:
        """One note as it is in the vault now, frontmatter included."""
        path = request.path
        if not self.layout.readable(path) or ".." in path.split("/"):
            raise PermissionDeniedError("only Markdown notes of the vault can be read")
        token = await self._app.installation_token(self._policy.repository, TokenPurpose.WATCH)
        try:
            sha = await self._require_writer().current_sha(token, path, self._policy.branch)
            text = "" if sha is None else await self._vault.text(token, sha)
        finally:
            await self._app.revoke(token)
        if sha is None:
            return NoteContent(path=path, exists=False)
        return NoteContent(path=path, exists=True, text=text[:NOTE_CHARS])

    async def write_note(self, task_id: uuid.UUID, request: NoteWrite) -> NoteWritten:
        """Create or replace a public note, one of enikk's reference notes or, for a role
        granted ``vault-private``, one of the owner's unpublished notes. Refuses to
        publish or unpublish a note."""
        task, _ = await self._origin(task_id)
        path = request.path
        reference = self.layout.is_reference(path)
        private = self.layout.private(path)
        if private and not self._grants(task, PlatformTool.VAULT_PRIVATE):
            raise PermissionDeniedError(
                f"role '{task.agent}' may not write the owner's unpublished notes"
            )
        if not (reference or private or self.layout.publishable(path)):
            raise PermissionDeniedError(
                "notes go under the vault outside Private, templates and hidden folders, "
                f"or under {self.layout.reference_dir}"
            )
        _refuse_secrets(request.text)
        draft = is_draft(request.text)
        if reference and not draft:
            raise PermissionDeniedError(
                "a reference note under enikk's folder must have draft: true in its frontmatter"
            )
        token = await self._token()
        try:
            writer = self._require_writer()
            current = await writer.current_sha(token, path, self._policy.branch)
            if current is None and not draft and not private:
                raise PermissionDeniedError(
                    "a new note must have draft: true in its frontmatter; the owner publishes it"
                )
            if (
                current is not None
                and not private
                and is_draft(await self._vault.text(token, current)) != draft
            ):
                raise PermissionDeniedError(
                    "a write cannot change the note's draft flag; the owner publishes "
                    "or unpublishes it"
                )
            action: Literal["created", "updated"] = "created" if current is None else "updated"
            prefix = "reference" if reference else "notes" if private else "blog"
            message = f"{prefix}: {action} {PurePosixPath(path).stem}"
            if draft or private:
                message += " [skip ci]"
            sha = await writer.put(token, path, request.text, message, self._policy.branch)
        finally:
            await self._app.revoke(token)
        note = parse_note(path, sha, request.text, self.layout, self._clock.now())
        async with self._uow() as uow:
            await uow.vault.upsert(note)
            await uow.commit()
        _log.info("public note %s: %s", action, path)
        return NoteWritten(action=action, path=path, draft=draft or private)

    # ------------------------------------------- changes that need the owner (ADR-0051 §4)

    async def request_change(
        self, task_id: uuid.UUID, request: NoteChangeRequest
    ) -> NoteChangeCreated:
        """Ask the owner, in the approval channel, to publish, unpublish, delete or move a
        note. Nothing in the vault changes until the owner approves."""
        task, session = await self._origin(task_id)
        if not self._grants(task, PlatformTool.VAULT_PRIVATE):
            raise PermissionDeniedError(
                f"role '{task.agent}' may not ask to publish, unpublish, delete or move notes"
            )
        _refuse_secrets(request.reason)
        self._check_targets(request.action, request.path, request.to)
        token = await self._app.installation_token(self._policy.repository, TokenPurpose.WATCH)
        try:
            writer = self._require_writer()
            sha = await writer.current_sha(token, request.path, self._policy.branch)
            if sha is None:
                raise NotFoundError(f"there is no note at {request.path}")
            if request.to is not None and await writer.current_sha(
                token, request.to, self._policy.branch
            ):
                raise InvalidRequestError(f"there is already a note at {request.to}")
            text = await self._vault.text(token, sha)
        finally:
            await self._app.revoke(token)
        before = self._published(request.path, text)
        after = self._published_after(request.action, request.path, request.to, text)
        if request.action is NoteChangeAction.PUBLISH and before:
            raise InvalidRequestError(f"{request.path} is already published")
        if request.action is NoteChangeAction.PUBLISH and not after:
            raise InvalidRequestError(f"{request.path} is in a folder the blog leaves out")
        if request.action is NoteChangeAction.UNPUBLISH and not before:
            raise InvalidRequestError(f"{request.path} is not published")
        now = self._clock.now()
        change = NoteChange(
            id=self._new_id(),
            action=request.action,
            path=request.path,
            to=request.to,
            reason=request.reason,
            blob_sha=sha,
            task_id=task.id,
            created_at=now,
        )
        notice = NoteChangeRequested(
            change_id=change.id,
            action=change.action,
            path=change.path,
            to=change.to,
            reason=change.reason,
            published_before=before,
            published_after=after,
            excerpt=_excerpt(text),
            origin=session.outbox_conversation,
        )
        async with self._uow() as uow:
            await uow.note_changes.add(change)
            await uow.notices.add(Notice(id=self._new_id(), body=notice, created_at=now))
            await uow.commit()
        _log.info("note change %s of %s asked by task %s", change.action, change.path, task.id)
        return NoteChangeCreated(change_id=change.id)

    async def change_posted(
        self, change_id: uuid.UUID, posted: NoteChangePosted
    ) -> NoteChangeLinked:
        """The request is in the approval channel: tell the asking thread, with its link."""
        async with self._uow() as uow:
            change = await uow.note_changes.get_for_update(change_id)
            if change is None:
                raise NotFoundError(f"note change {change_id} not found")
            where = f": {posted.link}" if posted.link else "."
            told = await self._tell_change(
                uow,
                change,
                f"노트 {CHANGE_NAMES[change.action]} 승인 요청을 보냈습니다{where}",
            )
            await uow.commit()
        return NoteChangeLinked(told_origin=told)

    async def decide_change(
        self, change_id: uuid.UUID, decision: NoteChangeDecision
    ) -> NoteChangeResult:
        if self._owner is None or decision.decided_by != self._owner:
            raise PermissionDeniedError("only the owner approves changes to notes")
        async with self._uow() as uow:
            change = await uow.note_changes.get_for_update(change_id)
            if change is None:
                raise NotFoundError(f"note change {change_id} not found")
            if change.status is not NoteChangeStatus.REQUESTED:
                return NoteChangeResult(
                    status=change.status, changed=False, detail="이미 처리된 요청입니다."
                )
            now = self._clock.now()
            name = f"{CHANGE_NAMES[change.action]}({change.path})"
            if not decision.apply:
                decided = change.decide(NoteChangeStatus.REJECTED, now, decision.decided_by)
                detail = f"노트 {name} 요청이 거절됐습니다. 볼트는 바뀌지 않았습니다."
            else:
                decided, detail = await self._apply(uow, change, now, decision.decided_by)
            await uow.note_changes.update(decided)
            await self._tell_change(uow, decided, detail)
            await uow.commit()
        _log.info("note change %s: %s by the owner", change_id, decided.status)
        return NoteChangeResult(status=decided.status, changed=True, detail=detail)

    async def _apply(
        self, uow: UnitOfWork, change: NoteChange, now: datetime, by: str
    ) -> tuple[NoteChange, str]:
        name = f"{CHANGE_NAMES[change.action]}({change.path})"
        token = await self._token()
        try:
            writer = self._require_writer()
            current = await writer.current_sha(token, change.path, self._policy.branch)
            if current != change.blob_sha:
                return (
                    change.decide(NoteChangeStatus.STALE, now, by),
                    f"노트 {name}: 요청 뒤 노트가 바뀌었거나 사라져 실행하지 않았습니다. "
                    "다시 요청해 주세요.",
                )
            text = await self._vault.text(token, current)
            stem = PurePosixPath(change.path).stem
            if change.action is NoteChangeAction.MOVE and change.to is not None:
                if await writer.current_sha(token, change.to, self._policy.branch):
                    return (
                        change.decide(NoteChangeStatus.STALE, now, by),
                        f"노트 {name}: {change.to} 에 이미 노트가 있어 옮기지 않았습니다.",
                    )
                quiet = not (self._published(change.path, text) or self._published(change.to, text))
                message = _quiet(f"blog: move {stem}", quiet)
                sha = await writer.put(token, change.to, text, message, self._policy.branch)
                await writer.delete(token, change.path, message, self._policy.branch)
                await uow.vault.upsert(parse_note(change.to, sha, text, self.layout, now))
                await uow.vault.remove([change.path])
                done = f"노트를 옮겼습니다: {change.path} → {change.to}"
            elif change.action is NoteChangeAction.DELETE:
                quiet = not self._published(change.path, text)
                await writer.delete(
                    token, change.path, _quiet(f"blog: delete {stem}", quiet), self._policy.branch
                )
                await uow.vault.remove([change.path])
                done = f"노트를 지웠습니다: {change.path} (git 이력에 남아 있습니다)"
            else:
                publish = change.action is NoteChangeAction.PUBLISH
                changed = set_draft(text, draft=not publish)
                verb = "publish" if publish else "unpublish"
                sha = await writer.put(
                    token, change.path, changed, f"blog: {verb} {stem}", self._policy.branch
                )
                await uow.vault.upsert(parse_note(change.path, sha, changed, self.layout, now))
                done = (
                    f"노트를 발행했습니다: {change.path}"
                    if publish
                    else f"노트를 블로그에서 내렸습니다: {change.path}"
                )
        finally:
            await self._app.revoke(token)
        return change.decide(NoteChangeStatus.DONE, now, by), done

    def _check_targets(self, action: NoteChangeAction, path: str, to: str | None) -> None:
        def allowed(p: str) -> bool:
            return self.layout.publishable(p) or self.layout.private(p)

        if not allowed(path):
            raise PermissionDeniedError(
                f"{path} is not one of the owner's notes (enikk's folder and hidden folders "
                "are left out)"
            )
        if action is NoteChangeAction.MOVE:
            if to is None or not allowed(to) or to == path:
                raise InvalidRequestError("move needs a different 'to' among the owner's notes")
        elif to is not None:
            raise InvalidRequestError(f"'to' is only for move, not {action}")

    def _published(self, path: str, text: str) -> bool:
        return self.layout.publishable(path) and not is_draft(text)

    def _published_after(
        self, action: NoteChangeAction, path: str, to: str | None, text: str
    ) -> bool:
        if action is NoteChangeAction.PUBLISH:
            return self.layout.publishable(path)
        if action is NoteChangeAction.MOVE and to is not None:
            return self._published(to, text)
        return False

    def _new_id(self) -> uuid.UUID:
        if self._ids is None:
            raise DependencyUnavailableError("ids are not configured")
        return self._ids.new()

    async def _tell_change(self, uow: UnitOfWork, change: NoteChange, text: str) -> bool:
        """Post ``text`` in the asking thread and wake its session (ADR-0041 §4)."""
        task = await uow.tasks.get_for_update(change.task_id)
        session = None if task is None else await uow.sessions.get(task.session_id)
        if task is None or session is None:
            return False
        now = self._clock.now()
        await _post(uow, task, session, OutboundKind.REPLY, text, self._new_id(), now)
        if self._waker is not None and change.status in NOTE_CHANGE_STATES:
            await self._waker.wake(
                uow,
                session,
                key=f"note:{change.id}:{change.status}",
                text=note_change_event_text(
                    CHANGE_NAMES[change.action], change.path, change.status, text
                ),
            )
        return True

    def _grants(self, task: Task, tool: PlatformTool) -> bool:
        definition = None if self._agents is None else self._agents.get(task.agent)
        return definition is not None and tool in definition.tools.platform

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

    async def _tell(self, task: Task, session: Session, kind: OutboundKind, text: str) -> None:
        """Mark the owner's message (ADR-0042); ``text`` stays as the record, unposted."""
        if self._ids is None:
            return
        async with self._uow() as uow:
            await _post(uow, task, session, kind, text, self._ids.new(), self._clock.now())
            await uow.commit()

    def _short(self, path: str) -> str:
        return path.removeprefix(self.layout.notes_root)


async def _post(
    uow: UnitOfWork,
    task: Task,
    session: Session,
    kind: OutboundKind,
    text: str,
    message_id: uuid.UUID,
    now: datetime,
) -> None:
    await uow.outbox.add(
        OutboxMessage(
            id=message_id,
            task_id=task.id,
            source_event_id=task.source_event_id,
            conversation=session.outbox_conversation,
            kind=kind,
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
    if any(looks_secret(t) for t in texts):
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
