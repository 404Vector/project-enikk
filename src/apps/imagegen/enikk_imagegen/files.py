"""Images on the runtime's shared volume (ADR-0043): read inputs from the calling
thread's own directories, save results to its ``tmp/<session>/images``, and keep each
style's reference images in ``styles/<style>``."""

from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING, Final

from enikk_imagegen.gemini import InlineImage
from enikk_kernel.errors import EnikkError

if TYPE_CHECKING:
    import uuid
    from datetime import datetime

MAX_INPUT_BYTES: Final = 7 * 1024 * 1024
MAX_STYLE_REFERENCES: Final = 6
"""Reference images one style may hold; they are sent with every request in it."""
MIME_BY_SUFFIX: Final = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
}
SUFFIX_BY_MIME: Final = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp"}


class ImageFileError(EnikkError):
    """An input image cannot be used, or a result cannot be saved."""


class Workspace:
    def __init__(self, root: Path) -> None:
        self._root = root

    def _thread_roots(self, session: uuid.UUID) -> tuple[Path, ...]:
        return (self._root / "tmp" / str(session), self._root / "worktrees" / str(session))

    def output_dir(self, session: uuid.UUID) -> Path:
        return self._root / "tmp" / str(session) / "images"

    def read(self, session: uuid.UUID, path: str) -> InlineImage:
        """An image under the thread's own tmp or worktree; nothing else is read."""
        candidate = Path(path)
        if not candidate.is_absolute():
            candidate = self.output_dir(session) / candidate
        resolved = candidate.resolve()
        if not any(resolved.is_relative_to(root.resolve()) for root in self._thread_roots(session)):
            raise ImageFileError(
                f"{path}: only images under this thread's tmp or worktree can be used"
            )
        mime = MIME_BY_SUFFIX.get(resolved.suffix.lower())
        if mime is None:
            raise ImageFileError(f"{path}: use a .png, .jpg or .webp file")
        try:
            size = resolved.stat().st_size
            if size > MAX_INPUT_BYTES:
                raise ImageFileError(f"{path}: larger than {MAX_INPUT_BYTES // (1024 * 1024)} MiB")
            data = resolved.read_bytes()
        except OSError as exc:
            raise ImageFileError(f"{path}: cannot read ({exc.strerror})") from exc
        return InlineImage(mime_type=mime, data=data)

    def style_dir(self, style: str) -> Path:
        return self._root / "styles" / style

    def style_references(self, style: str) -> list[InlineImage]:
        """The style's reference images, by file name."""
        directory = self.style_dir(style)
        if not directory.is_dir():
            return []
        files = sorted(
            p for p in directory.iterdir() if p.is_file() and p.suffix.lower() in MIME_BY_SUFFIX
        )
        return [
            InlineImage(mime_type=MIME_BY_SUFFIX[p.suffix.lower()], data=p.read_bytes())
            for p in files[:MAX_STYLE_REFERENCES]
        ]

    def add_style_reference(self, session: uuid.UUID, style: str, path: str) -> Path:
        """Copy one of the thread's images into the style's references."""
        image = self.read(session, path)
        directory = self.style_dir(style)
        try:
            directory.mkdir(parents=True, exist_ok=True)
            existing = [p for p in directory.iterdir() if p.suffix.lower() in MIME_BY_SUFFIX]
            if len(existing) >= MAX_STYLE_REFERENCES:
                raise ImageFileError(
                    f"{style} already has {MAX_STYLE_REFERENCES} reference images; remove one first"
                )
            stem = _slug(Path(path).stem)
            suffix = SUFFIX_BY_MIME.get(image.mime_type, ".png")
            target = directory / f"{stem}{suffix}"
            counter = 1
            while target.exists():
                counter += 1
                target = directory / f"{stem}-{counter}{suffix}"
            target.write_bytes(image.data)
        except OSError as exc:
            raise ImageFileError(f"cannot save the reference image ({exc.strerror})") from exc
        return target

    def remove_style_reference(self, style: str, file_name: str) -> None:
        directory = self.style_dir(style)
        target = (directory / file_name).resolve()
        if target.parent != directory.resolve() or not target.is_file():
            raise ImageFileError(f"{style} has no reference image named {file_name}")
        try:
            target.unlink()
        except OSError as exc:
            raise ImageFileError(f"cannot remove the reference image ({exc.strerror})") from exc

    def style_reference_names(self, style: str) -> list[str]:
        directory = self.style_dir(style)
        if not directory.is_dir():
            return []
        return sorted(
            p.name
            for p in directory.iterdir()
            if p.is_file() and p.suffix.lower() in MIME_BY_SUFFIX
        )

    def save(self, session: uuid.UUID, image: InlineImage, *, name: str, now: datetime) -> Path:
        directory = self.output_dir(session)
        suffix = SUFFIX_BY_MIME.get(image.mime_type, ".png")
        stem = f"{now:%Y%m%d-%H%M%S}-{_slug(name)}"
        target = directory / f"{stem}{suffix}"
        try:
            directory.mkdir(parents=True, exist_ok=True)
            counter = 1
            while target.exists():
                counter += 1
                target = directory / f"{stem}-{counter}{suffix}"
            target.write_bytes(image.data)
        except OSError as exc:
            raise ImageFileError(f"cannot save the image ({exc.strerror})") from exc
        return target


def _slug(name: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9_-]+", "-", name).strip("-").lower()
    return slug[:40] or "image"
