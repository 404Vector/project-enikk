"""A session's pull request, pushed by core (ADR-0037 §6b, 2b).

The runtime sends the session's commits as a git bundle; core checks them, pushes them
from its own clone to ``agent/<session>`` and opens or updates the PR. The model never
holds a GitHub token.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Final

from pydantic import Field, StringConstraints

from enikk_contracts.base import Contract

MAX_BUNDLE_BYTES: Final = 4 * 1024 * 1024
MAX_BUNDLE_BASE64: Final = (MAX_BUNDLE_BYTES + 2) // 3 * 4
MAX_COMMITS: Final = 50
MAX_FILES: Final = 300

CommitSha = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{40}$")]
PullTitle = Annotated[str, StringConstraints(min_length=1, max_length=200, strip_whitespace=True)]
PullBody = Annotated[str, StringConstraints(max_length=20_000)]


class PullSubmission(Contract):
    """The session's branch as a bundle of the commits after ``base``."""

    lease_id: uuid.UUID
    bundle: Annotated[
        str,
        StringConstraints(min_length=4, max_length=MAX_BUNDLE_BASE64, pattern=r"^[A-Za-z0-9+/=]+$"),
    ]
    """Base64 of ``git bundle create`` output, at most ``MAX_BUNDLE_BYTES`` decoded."""
    base: CommitSha
    """The merge base with main the bundle starts from."""
    head: CommitSha
    """The tip of ``session/<session>`` in the bundle."""
    title: PullTitle | None = None
    """Omitted: the first commit's subject for a new PR; unchanged for an open one."""
    body: PullBody | None = None


class PullSubmitted(Contract):
    number: int = Field(ge=1)
    url: Annotated[str, StringConstraints(min_length=1, max_length=500)]
    branch: Annotated[str, StringConstraints(min_length=1, max_length=200)]
    head: CommitSha
    """The pushed commit (after the author was rewritten)."""
    commits: int = Field(ge=1)
    created: bool
    """False when an open PR of the same thread was updated."""
