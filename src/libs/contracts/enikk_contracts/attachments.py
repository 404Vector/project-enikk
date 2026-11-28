"""Files the owner attached to a message, carried from the connector to the task (ADR-0042 2b)."""

from __future__ import annotations

import uuid
from enum import StrEnum
from typing import Annotated, Final, Self

from pydantic import Field, StringConstraints, model_validator

from enikk_contracts.base import Contract
from enikk_contracts.thread_actions import MAX_FILE_BYTES, Base64Text, FileName

MAX_ATTACHMENTS: Final = 10
"""Attachments kept from one message; the rest are named as skipped."""
MAX_ATTACHMENT_BYTES: Final = MAX_FILE_BYTES

FailureDetail = Annotated[str, StringConstraints(min_length=1, max_length=200)]


class AttachmentInfo(Contract):
    """One attachment as the connector saw it in the message."""

    name: FileName
    size: Annotated[int, Field(ge=0)]


class AttachmentState(StrEnum):
    PENDING = "pending"
    """The connector has not uploaded it yet."""
    READY = "ready"
    FAILED = "failed"


class AttachmentUpload(Contract):
    """The connector's upload of one attachment: its bytes, or why it has none."""

    content_base64: Base64Text | None = None
    failure: FailureDetail | None = None

    @model_validator(mode="after")
    def _one_of(self) -> Self:
        if (self.content_base64 is None) == (self.failure is None):
            raise ValueError("give either content_base64 or failure")
        return self


class AttachmentStored(Contract):
    state: AttachmentState


class TaskAttachment(Contract):
    """An attachment of the leased task; the runtime fetches it by ``index``."""

    index: Annotated[int, Field(ge=0, lt=MAX_ATTACHMENTS)]
    name: FileName
    size: Annotated[int, Field(ge=0)]


class AttachmentFetch(Contract):
    lease_id: uuid.UUID


class AttachmentContent(Contract):
    """``content_base64`` only when ``READY``; ``detail`` only when ``FAILED``."""

    state: AttachmentState
    content_base64: Base64Text | None = None
    detail: FailureDetail | None = None
