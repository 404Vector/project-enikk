"""Process entry point: ``enikk-dev-agent``. Exits when its one task is done."""

from __future__ import annotations

import asyncio
import logging
import sys

from enikk_dev_agent.app import DevAgentApp
from enikk_dev_agent.settings import DevAgentSettings
from enikk_dev_agent.workspace import GitWorkspace
from enikk_kernel.errors import EnikkError
from enikk_kernel.logging import configure_logging
from enikk_kernel.process import SubprocessRunner

_log = logging.getLogger(__name__)


def main() -> None:
    settings = DevAgentSettings()  # task_id comes from ENIKK_DEV_TASK_ID
    configure_logging(verbose=settings.verbose)
    workspace = GitWorkspace(SubprocessRunner(), settings.workspace, settings.tool_home)
    try:
        asyncio.run(DevAgentApp(settings, workspace).run())
    except EnikkError as error:
        _log.error("error: %s", error)
        sys.exit(error.exit_code)
