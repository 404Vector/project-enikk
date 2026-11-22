"""Text that looks like a secret (I-07): refused in memory, masked in diagnostics."""

from __future__ import annotations

import re
from typing import Final

SECRET: Final = re.compile(
    r"(gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|xox[abprs]-[A-Za-z0-9-]{10,}"
    r"|xapp-[A-Za-z0-9-]{10,}|sk-ant-[A-Za-z0-9_-]{10,}|sk-[A-Za-z0-9]{20,}"
    r"|AKIA[0-9A-Z]{16}|AIza[0-9A-Za-z_-]{30,}|eyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{10,}"
    r"\.[A-Za-z0-9_-]{10,}|-----BEGIN [A-Z ]*PRIVATE KEY-----)"
)
MASK: Final = "[가림]"


def looks_secret(text: str) -> bool:
    return SECRET.search(text) is not None


def redact(text: str) -> str:
    return SECRET.sub(MASK, text)
