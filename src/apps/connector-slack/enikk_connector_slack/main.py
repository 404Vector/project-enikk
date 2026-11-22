"""Process entry point: ``enikk-connector-slack``."""

from __future__ import annotations

import asyncio
import logging
import sys

from enikk_connector_slack.app import ConnectorSlackApp
from enikk_connector_slack.settings import ConnectorSlackSettings
from enikk_kernel.errors import EnikkError
from enikk_kernel.logging import configure_logging

_log = logging.getLogger(__name__)


def main() -> None:
    settings = ConnectorSlackSettings()
    configure_logging(verbose=settings.verbose)
    try:
        asyncio.run(ConnectorSlackApp(settings).run())
    except EnikkError as error:
        _log.error("error: %s", error)
        sys.exit(error.exit_code)
