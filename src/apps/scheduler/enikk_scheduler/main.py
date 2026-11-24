"""Process entry point: ``enikk-scheduler``."""

from __future__ import annotations

import logging
import sys

import uvicorn

from enikk_core_client import CoreClient, FileTokenSource
from enikk_kernel.errors import EnikkError
from enikk_kernel.logging import configure_logging
from enikk_scheduler.app import create_app
from enikk_scheduler.settings import SchedulerSettings
from enikk_scheduler.tools import SERVER_NAME

_log = logging.getLogger(__name__)


def main() -> None:
    settings = SchedulerSettings()
    configure_logging(verbose=settings.verbose)
    try:
        core = CoreClient.connect(settings.core_url, FileTokenSource(settings.token_file))
        app = create_app(core, allowed_hosts=settings.allowed_hosts)
        _log.info("starting %s port=%d", SERVER_NAME, settings.port)
        uvicorn.run(app, host=settings.host, port=settings.port, log_config=None)
    except EnikkError as error:
        _log.error("error: %s", error)
        sys.exit(error.exit_code)
