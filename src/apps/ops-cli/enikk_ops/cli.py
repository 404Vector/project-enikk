"""Command-line interface. Parses input, calls use cases, renders output."""

from __future__ import annotations

import sys
from typing import Annotated

import typer

from enikk_kernel.errors import EnikkError
from enikk_kernel.logging import configure_logging
from enikk_ops.container import Container

app = typer.Typer(no_args_is_help=True, help="enikk ops: cluster bootstrap and local tools.")
argocd_app = typer.Typer(no_args_is_help=True, help="Argo CD (GitOps controller).")
app.add_typer(argocd_app, name="argocd")
smoke_app = typer.Typer(no_args_is_help=True, help="End-to-end checks of deployed services.")
app.add_typer(smoke_app, name="smoke")

DryRun = Annotated[bool, typer.Option("--dry-run", help="Print mutating commands only.")]
Verbose = Annotated[bool, typer.Option("--verbose", "-v", help="Debug logging.")]


@app.callback()
def _root(ctx: typer.Context, dry_run: DryRun = False, verbose: Verbose = False) -> None:
    configure_logging(verbose=verbose)
    ctx.obj = Container(dry_run=dry_run)


def _container(ctx: typer.Context) -> Container:
    container = ctx.obj
    if not isinstance(container, Container):
        raise RuntimeError("container not initialised")
    return container


@argocd_app.command("bootstrap")
def argocd_bootstrap(ctx: typer.Context) -> None:
    """Install Argo CD and register the root Application from git."""
    _container(ctx).argocd.bootstrap()
    typer.echo("Argo CD installed. The cluster now follows deploy/argocd/ on main.")


@argocd_app.command("status")
def argocd_status(ctx: typer.Context) -> None:
    """List Argo CD Applications and their sync / health state."""
    typer.echo(_container(ctx).argocd.status())


@argocd_app.command("ui")
def argocd_ui(ctx: typer.Context) -> None:
    """Port-forward the Argo CD UI to localhost."""
    container = _container(ctx)
    settings = container.settings.argocd
    typer.echo(f"Argo CD UI: http://localhost:{settings.ui_port}  (user: admin)")
    typer.echo(
        "Initial password: kubectl -n "
        f"{settings.namespace} get secret argocd-initial-admin-secret "
        "-o jsonpath='{.data.password}' | base64 -d"
    )
    raise typer.Exit(container.argocd.open_ui())


@smoke_app.command("core")
def smoke_core(ctx: typer.Context) -> None:
    """Drive event -> task -> result -> outbox against the deployed core with real tokens."""
    typer.echo("enikk-core smoke test")
    _container(ctx).core_smoke.run()
    typer.echo("all checks passed")


def main() -> None:
    try:
        app()
    except EnikkError as error:
        typer.secho(f"error: {error}", fg=typer.colors.RED, err=True)
        sys.exit(error.exit_code)
