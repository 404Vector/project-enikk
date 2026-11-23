"""Text that looks like a secret (I-07). Lives in the kernel so engines can mask too."""

from __future__ import annotations

from enikk_kernel.redaction import MASK, SECRET, looks_secret, redact

__all__ = ["MASK", "SECRET", "looks_secret", "redact"]
