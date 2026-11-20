"""Process entry point and composition root."""

from __future__ import annotations

import logging

import uvicorn

from enikk_core.api import SERVICE_NAME, create_app
from enikk_core.settings import CoreSettings
from enikk_kernel.logging import configure_logging

_log = logging.getLogger(__name__)


def main() -> None:
    settings = CoreSettings()
    configure_logging(verbose=settings.verbose)
    _log.info("starting %s version=%s port=%d", SERVICE_NAME, settings.version, settings.port)
    uvicorn.run(create_app(settings), host=settings.host, port=settings.port, log_config=None)
