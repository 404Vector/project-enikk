"""Save the owner's attachments of a task in the session's tmp (ADR-0042 2b)."""

from __future__ import annotations

import base64
import binascii
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Final

from enikk_contracts.attachments import AttachmentFetch, AttachmentState
from enikk_core_client.errors import CoreApiError
from enikk_kernel.models import FrozenModel

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from enikk_contracts.attachments import TaskAttachment
    from enikk_contracts.tasks import TaskAssignment
    from enikk_runtime.ports import TaskSource

    Sleep = Callable[[float], Awaitable[None]]

ATTACHMENTS_DIR: Final = "attachments"
WAIT_SECONDS: Final = 60.0
"""How long a task waits for the connector to upload its attachments."""
POLL_SECONDS: Final = 2.0

_log = logging.getLogger(__name__)


class SavedAttachment(FrozenModel):
    """``path`` when saved; otherwise ``detail`` says why not."""

    name: str
    path: Path | None = None
    detail: str | None = None


async def save_attachments(
    core: TaskSource,
    assignment: TaskAssignment,
    tmp_dir: Path,
    sleep: Sleep,
    *,
    wait_seconds: float = WAIT_SECONDS,
    poll_seconds: float = POLL_SECONDS,
) -> tuple[SavedAttachment, ...]:
    """Fetch each attachment, waiting for pending ones, and write it under
    ``tmp_dir/attachments/<task>/``. Never raises: a failure is kept as a detail."""
    folder = tmp_dir / ATTACHMENTS_DIR / str(assignment.task_id)[:8]
    fetch = AttachmentFetch(lease_id=assignment.lease_id)
    saved: dict[int, SavedAttachment] = {}
    waited = 0.0
    while True:
        for attachment in assignment.attachments:
            if attachment.index not in saved:
                found = await _fetch_one(core, assignment, attachment, fetch, folder)
                if found is not None:
                    saved[attachment.index] = found
        if len(saved) == len(assignment.attachments) or waited >= wait_seconds:
            break
        await sleep(poll_seconds)
        waited += poll_seconds
    return tuple(
        saved.get(a.index, SavedAttachment(name=a.name, detail="아직 올라오지 않았다"))
        for a in assignment.attachments
    )


async def _fetch_one(
    core: TaskSource,
    assignment: TaskAssignment,
    attachment: TaskAttachment,
    fetch: AttachmentFetch,
    folder: Path,
) -> SavedAttachment | None:
    """The settled attachment, or ``None`` while it is pending."""
    try:
        content = await core.fetch_attachment(assignment.task_id, attachment.index, fetch)
    except CoreApiError as exc:
        _log.warning("task %s: attachment %d: %s", assignment.task_id, attachment.index, exc)
        return SavedAttachment(name=attachment.name, detail="core 에서 받지 못했다")
    if content.state is AttachmentState.PENDING:
        return None
    if content.state is AttachmentState.FAILED or content.content_base64 is None:
        return SavedAttachment(name=attachment.name, detail=content.detail or "받지 못했다")
    try:
        data = base64.b64decode(content.content_base64, validate=True)
    except binascii.Error:
        return SavedAttachment(name=attachment.name, detail="내용이 깨졌다")
    path = folder / attachment.name
    if path.resolve().parent != folder.resolve():
        return SavedAttachment(name=attachment.name, detail="쓸 수 없는 이름이다")
    try:
        folder.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    except OSError as exc:
        _log.warning(
            "task %s: attachment %d not saved: %s", assignment.task_id, attachment.index, exc
        )
        return SavedAttachment(name=attachment.name, detail="저장하지 못했다")
    return SavedAttachment(name=attachment.name, path=path)
