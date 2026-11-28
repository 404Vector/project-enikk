"""A task's attachments are saved in the session's tmp and named in its input (ADR-0042 2b)."""

from __future__ import annotations

import base64
from typing import TYPE_CHECKING

import pytest

from enikk_contracts.attachments import AttachmentContent, AttachmentState, TaskAttachment
from enikk_kernel.testing import ManualClock
from enikk_runtime.attachments import SavedAttachment, save_attachments
from enikk_runtime.testing import FakeCore

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.anyio

PNG = b"\x89PNG-image"
PENDING = AttachmentContent(state=AttachmentState.PENDING)
READY = AttachmentContent(
    state=AttachmentState.READY, content_base64=base64.b64encode(PNG).decode()
)


async def _no_sleep(_: float) -> None:
    return None


async def test_attachments_are_saved_after_waiting_for_the_upload(tmp_path: Path) -> None:
    core = FakeCore(ManualClock())
    assignment = core.assign().model_copy(
        update={
            "attachments": (
                TaskAttachment(index=0, name="a.png", size=len(PNG)),
                TaskAttachment(index=1, name="b.zip", size=99),
            )
        }
    )
    core.attachments[(assignment.task_id, 0)] = [PENDING, PENDING, READY]
    core.attachments[(assignment.task_id, 1)] = [
        AttachmentContent(state=AttachmentState.FAILED, detail="larger than 10485760 bytes")
    ]

    saved = await save_attachments(core, assignment, tmp_path, _no_sleep)

    first, second = saved
    assert first.path is not None
    assert first.path.read_bytes() == PNG
    assert first.path.is_relative_to(tmp_path / "attachments")
    assert second == SavedAttachment(name="b.zip", detail="larger than 10485760 bytes")


async def test_an_attachment_never_uploaded_is_named_after_the_wait(tmp_path: Path) -> None:
    core = FakeCore(ManualClock())
    assignment = core.assign().model_copy(
        update={"attachments": (TaskAttachment(index=0, name="a.png", size=1),)}
    )
    core.attachments[(assignment.task_id, 0)] = [PENDING]

    saved = await save_attachments(
        core, assignment, tmp_path, _no_sleep, wait_seconds=4, poll_seconds=2
    )

    assert saved == (SavedAttachment(name="a.png", detail="아직 올라오지 않았다"),)


async def test_a_core_error_is_kept_as_a_detail(tmp_path: Path) -> None:
    core = FakeCore(ManualClock())
    assignment = core.assign().model_copy(
        update={"attachments": (TaskAttachment(index=0, name="a.png", size=1),)}
    )

    saved = await save_attachments(core, assignment, tmp_path, _no_sleep)

    assert saved == (SavedAttachment(name="a.png", detail="core 에서 받지 못했다"),)
