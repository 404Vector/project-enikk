"""Logging setup. Called once by each app's entry point."""

from __future__ import annotations

import logging
import sys


def configure_logging(*, verbose: bool = False) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(message)s" if not verbose else "%(levelname)-7s %(name)s: %(message)s",
        stream=sys.stderr,
        force=True,
    )
    for noisy in ("httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.DEBUG if verbose else logging.WARNING)
