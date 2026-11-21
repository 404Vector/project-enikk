"""Command-line interface. Parses input, calls use cases, renders output."""

from __future__ import annotations

import sys
from typing import Annotated

import typer
from pydantic import SecretStr

from enikk_contracts.credentials import CredentialName  # noqa: TC001 - typer reads annotations
from enikk_kernel.errors import EnikkError
from enikk_kernel.logging import configure_logging
from enikk_ops.container import Container

app = typer.Typer(no_args_is_help=True, help="enikk ops: cluster bootstrap and local tools.")
argocd_app = typer.Typer(no_args_is_help=True, help="Argo CD (GitOps controller).")
app.add_typer(argocd_app, name="argocd")
smoke_app = typer.Typer(no_args_is_help=True, help="End-to-end checks of deployed services.")
app.add_typer(smoke_app, name="smoke")
secret_app = typer.Typer(
    no_args_is_help=True, help="Credentials kept on this PC and handed out by core."
)
app.add_typer(secret_app, name="secret")

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


@secret_app.command("set")
def secret_set(ctx: typer.Context, name: CredentialName) -> None:
    """Store a credential. The value is read from a hidden prompt, never from argv."""
    value = typer.prompt(name.value, hide_input=True).strip()
    if not value:
        raise typer.BadParameter("the value must not be empty")
    file = _container(ctx).secret_file
    file.set(name, SecretStr(value))
    typer.echo(f"{name} saved to {file.path}. Apply it with: enikk-ops secret sync")


@secret_app.command("unset")
def secret_unset(ctx: typer.Context, name: CredentialName) -> None:
    """Remove a credential from the local file."""
    if _container(ctx).secret_file.unset(name):
        typer.echo(f"{name} removed. Apply it with: enikk-ops secret sync")
    else:
        typer.echo(f"{name} was not set")


@secret_app.command("list")
def secret_list(ctx: typer.Context) -> None:
    """Show which credentials are set. Values are never printed."""
    file = _container(ctx).secret_file
    typer.echo(f"file: {file.path}")
    for status in file.statuses():
        state = f"set {status.updated_at:%Y-%m-%d %H:%M}" if status.updated_at else "-"
        typer.echo(f"  {status.name.value:<22} {state}")


@secret_app.command("sync")
def secret_sync(ctx: typer.Context) -> None:
    """Write every local credential into core's Secret; names not in the file are removed."""
    synced = _container(ctx).credential_sync.run()
    typer.echo(f"synced {len(synced)} credentials: {', '.join(synced)}")


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
