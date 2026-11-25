"""What a session may do in the thread it answers, besides replying (ADR-0042)."""

from __future__ import annotations

import uuid
from typing import Annotated, Final, Literal

from pydantic import StringConstraints

from enikk_contracts.base import Contract

MAX_FILE_BYTES: Final = 10 * 1024 * 1024
MAX_FILES_PER_TASK: Final = 10

FileName = Annotated[
    str, StringConstraints(min_length=1, max_length=200, pattern=r"^[^/\\\x00-\x1f]+$")
]
FileTitle = Annotated[str, StringConstraints(min_length=1, max_length=200, strip_whitespace=True)]
EmojiName = Annotated[str, StringConstraints(pattern=r"^[a-z0-9_+'\-]{1,100}$")]
Base64Text = Annotated[
    str, StringConstraints(min_length=1, max_length=(MAX_FILE_BYTES + 2) // 3 * 4)
]


class ThreadFileRequest(Contract):
    """Post a file of the session's workspace to the thread the task answers."""

    lease_id: uuid.UUID
    filename: FileName
    title: FileTitle
    content_base64: Base64Text


class ThreadReactionRequest(Contract):
    """Put an emoji on the owner's message that the task answers."""

    lease_id: uuid.UUID
    name: EmojiName


class ThreadActionDone(Contract):
    action: Literal["file", "reaction"]
