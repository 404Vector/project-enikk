"""Process entry point: ``enikk-runtime-claude``."""

from __future__ import annotations

import asyncio
import logging
import sys

from enikk_kernel.errors import EnikkError
from enikk_kernel.logging import configure_logging
from enikk_runtime_claude.app import RuntimeClaudeApp
from enikk_runtime_claude.settings import RuntimeClaudeSettings

_log = logging.getLogger(__name__)


def main() -> None:
    settings = RuntimeClaudeSettings()
    configure_logging(verbose=settings.verbose)
    try:
        asyncio.run(RuntimeClaudeApp(settings).run())
    except EnikkError as error:
        _log.error("error: %s", error)
        sys.exit(error.exit_code)
