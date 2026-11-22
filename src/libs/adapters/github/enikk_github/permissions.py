"""Fixed permission sets per token purpose (ADR-0019 §2)."""

from __future__ import annotations

from enum import StrEnum
from typing import Final


class Access(StrEnum):
    READ = "read"
    WRITE = "write"


class TokenPurpose(StrEnum):
    DEVELOP = "develop"
    """Handed to the dev agent Pod. Never records approvals or changes settings."""
    APPROVE = "approve"
    """Core's approval module: records ``enikk/owner-approval`` and enables auto-merge."""
    WATCH = "watch"
    """Core's pull request and deployment watchers. Read-only."""
    MEMORY = "memory"
    """Core's memory: writes notes into the owner's vault repository (ADR-0028)."""
    SETUP = "setup"
    """Core's repository onboarding: applies the standard ruleset."""


Permissions = dict[str, Access]

_FIXED: Final[dict[TokenPurpose, Permissions]] = {
    TokenPurpose.DEVELOP: {
        "metadata": Access.READ,
        "contents": Access.WRITE,
        "pull_requests": Access.WRITE,
        "checks": Access.READ,
        "statuses": Access.READ,
    },
    TokenPurpose.APPROVE: {
        "metadata": Access.READ,
        "contents": Access.WRITE,
        "pull_requests": Access.WRITE,
        "statuses": Access.WRITE,
    },
    TokenPurpose.WATCH: {
        "metadata": Access.READ,
        "actions": Access.READ,
        "contents": Access.READ,
        "pull_requests": Access.READ,
        "checks": Access.READ,
        "statuses": Access.READ,
    },
    TokenPurpose.MEMORY: {
        "metadata": Access.READ,
        "contents": Access.WRITE,
    },
    TokenPurpose.SETUP: {
        "metadata": Access.READ,
        "administration": Access.WRITE,
    },
}

NEVER_FOR_DEVELOP: Final = frozenset({"statuses", "checks", "administration"})
"""Write access the dev agent must never get: it could approve itself or lift protection."""


def permissions_for(purpose: TokenPurpose, *, workflows: bool = False) -> Permissions:
    """The exact permissions of a token for ``purpose``.

    ``workflows`` adds write access to workflow files, for development tokens of
    repositories whose policy allows it. The platform's own repository never does.
    """
    if workflows and purpose is not TokenPurpose.DEVELOP:
        raise ValueError("only development tokens may write workflows")
    permissions = dict(_FIXED[purpose])
    if workflows:
        permissions["workflows"] = Access.WRITE
    if purpose is TokenPurpose.DEVELOP:
        leaked = {k for k, v in permissions.items() if v is Access.WRITE} & NEVER_FOR_DEVELOP
        if leaked:
            raise AssertionError(f"development token would write {sorted(leaked)}")
    return permissions


def required_permissions(*, workflows: bool) -> Permissions:
    """What the App installation must grant so that every purpose can be served."""
    required: Permissions = {}
    sets = [_FIXED[p] for p in TokenPurpose]
    if workflows:
        sets.append({"workflows": Access.WRITE})
    for permissions in sets:
        for name, access in permissions.items():
            if required.get(name) is not Access.WRITE:
                required[name] = access
    return required
