"""Process entry point and composition root."""

from __future__ import annotations

import uvicorn

from enikk_core.api import create_app
from enikk_core.settings import CoreSettings
from enikk_kernel.logging import configure_logging


def main() -> None:
    settings = CoreSettings()
    configure_logging(verbose=settings.verbose)
    uvicorn.run(create_app(settings), host=settings.host, port=settings.port, log_config=None)
