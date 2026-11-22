"""Process entry point: ``enikk-core serve``, ``migrate`` and ``selfcheck``."""

from __future__ import annotations

import asyncio
import logging
import sys

import typer
import uvicorn

from enikk_core.api import SERVICE_NAME, create_app
from enikk_core.container import build_services
from enikk_core.selfcheck import SelfCheck, SelfCheckSettings
from enikk_core.settings import CoreSettings
from enikk_kernel.errors import EnikkError
from enikk_kernel.logging import configure_logging
from enikk_persistence_sql.migrate import upgrade

_log = logging.getLogger(__name__)

cli = typer.Typer(no_args_is_help=True, help="enikk-core control plane.")


@cli.command()
def serve() -> None:
    """Run the API server."""
    settings = CoreSettings()
    configure_logging(verbose=settings.verbose)
    _log.info("starting %s version=%s port=%d", SERVICE_NAME, settings.version, settings.port)
    app = create_app(build_services(settings))
    uvicorn.run(app, host=settings.host, port=settings.port, log_config=None)


@cli.command()
def migrate() -> None:
    """Apply database migrations (run by the PreSync Job, ADR-0012 §5)."""
    settings = CoreSettings()
    configure_logging(verbose=settings.verbose)
    _log.info("applying migrations to %s", settings.database.host)
    upgrade(settings.database.to_config())
    _log.info("migrations applied")


@cli.command()
def selfcheck() -> None:
    """Check the deployed core through its Service (run by the PostSync Job, ADR-0023)."""
    configure_logging(verbose=False)
    asyncio.run(SelfCheck(SelfCheckSettings()).run())


def main() -> None:
    try:
        cli()
    except EnikkError as error:
        _log.error("error: %s", error)
        sys.exit(error.exit_code)
