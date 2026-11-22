"""Enikk's memory from the owner's vault (ADR-0028): sync the index, hand out notes.

The vault repository is the source. Every interval core lists the vault's files,
fetches the notes whose blob changed and updates its index; deleted notes leave
the index. Each task receives enikk's preferences, facts and unexpired work.
"""

from __future__ import annotations

import logging
import re
from datetime import date, datetime
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Final, Protocol

import yaml

from enikk_core_domain.errors import DependencyUnavailableError
from enikk_core_domain.memory import MAX_BODY, MemoryLayout, VaultNote, context_notes
from enikk_github.permissions import TokenPurpose
from enikk_kernel.models import FrozenModel

if TYPE_CHECKING:
    from enikk_contracts.memory import MemoryNote
    from enikk_core.github import GitHubAppApi
    from enikk_core_domain.ports import UnitOfWorkFactory
    from enikk_github.app import InstallationToken
    from enikk_github.vault import VaultFile
    from enikk_kernel.clock import Clock

_log = logging.getLogger(__name__)

_FRONTMATTER: Final = re.compile(r"\A---\r?\n(.*?)\r?\n---\r?\n?", re.S)
_HEADING: Final = re.compile(r"^#\s+(.+?)\s*$", re.M)


class VaultApi(Protocol):
    async def files(self, token: InstallationToken, branch: str = "main") -> list[VaultFile]: ...
    async def text(self, token: InstallationToken, sha: str) -> str: ...


class MemoryPolicy(FrozenModel):
    repository: str
    branch: str = "main"
    layout: MemoryLayout = MemoryLayout()
    context_chars: int = 4_000


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
    ) -> None:
        self._uow = uow
        self._app = app
        self._vault = vault
        self._policy = policy
        self._clock = clock

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
