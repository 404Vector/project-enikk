"""Files on the runtime's shared volume (ADR-0052 §2): uploads come from the calling
thread's own tmp or worktree, downloads go to its ``tmp/<session>/drive``."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from enikk_kernel.errors import EnikkError

if TYPE_CHECKING:
    import uuid


class LocalFileError(EnikkError):
    """A local file cannot be read or written."""


class Workspace:
    def __init__(self, root: Path) -> None:
        self._root = root

    def _thread_roots(self, session: uuid.UUID) -> tuple[Path, ...]:
        return (self._root / "tmp" / str(session), self._root / "worktrees" / str(session))

    def download_dir(self, session: uuid.UUID) -> Path:
        return self._root / "tmp" / str(session) / "drive"

    def source(self, session: uuid.UUID, path: str, *, max_bytes: int) -> tuple[Path, int]:
        """A file under the thread's own tmp or worktree, and its size."""
        candidate = Path(path)
        if not candidate.is_absolute():
            candidate = self._root / "tmp" / str(session) / candidate
        resolved = candidate.resolve()
        if not any(resolved.is_relative_to(root.resolve()) for root in self._thread_roots(session)):
            raise LocalFileError(f"{path}: only files under this thread's tmp or worktree")
        try:
            if not resolved.is_file():
                raise LocalFileError(f"{path}: not a file")
            size = resolved.stat().st_size
        except OSError as exc:
            raise LocalFileError(f"{path}: cannot read ({exc.strerror})") from exc
        if size > max_bytes:
            raise LocalFileError(f"{path}: larger than {max_bytes // (1024 * 1024)} MiB")
        return resolved, size

    def target(self, session: uuid.UUID, segments: tuple[str, ...]) -> Path:
        """Where a download of ``segments`` goes; its folders are created."""
        base = self.download_dir(session)
        target = base.joinpath(*segments)
        if not target.resolve().is_relative_to(base.resolve()):
            raise LocalFileError(f"{'/'.join(segments)}: outside this thread's drive folder")
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise LocalFileError(f"cannot create {target.parent} ({exc.strerror})") from exc
        return target
