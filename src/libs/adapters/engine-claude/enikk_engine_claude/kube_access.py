"""Read-only Kubernetes access from the shell (ADR-0041 §1, 3c).

The runtime's ServiceAccount may only read (no Secrets, no writes). Its projected token
stays outside the sandbox; the engine copies the current token and the cluster CA into
the run's home before the CLI starts, as a kubeconfig and for ``enikk-kube``. The home is
removed when the run ends. Like the session's GitHub token, this is a secret the
sandboxed tools can read, by the owner's decision of 2026-11-25.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Final

import httpx

from enikk_kernel.models import FrozenModel

TOKEN_FILE: Final = Path(".config/enikk/kube-token")
CA_FILE: Final = Path(".config/enikk/kube-ca.crt")
SERVER_FILE: Final = Path(".config/enikk/kube-server")
KUBECONFIG: Final = Path(".kube/config")
TIMEOUT_SECONDS: Final = 30.0


class KubeAccess(FrozenModel):
    """Where the runtime's read-only Kubernetes credentials are mounted."""

    token_file: Path
    ca_file: Path
    server: str = "https://kubernetes.default.svc"


def install(home: Path, access: KubeAccess) -> bool:
    """Copy the current token and CA into ``home``. False if they are not mounted."""
    if not access.token_file.exists() or not access.ca_file.exists():
        return False
    token = access.token_file.read_text(encoding="utf-8").strip()
    _write_private(home / TOKEN_FILE, token + "\n")
    _write_private(home / CA_FILE, access.ca_file.read_text(encoding="utf-8"))
    _write_private(home / SERVER_FILE, access.server + "\n")
    _write_private(
        home / KUBECONFIG,
        "apiVersion: v1\n"
        "kind: Config\n"
        "clusters:\n"
        f"  - name: local\n    cluster:\n      server: {access.server}\n"
        f"      certificate-authority: {home / CA_FILE}\n"
        "users:\n"
        f"  - name: enikk-runtime\n    user:\n      tokenFile: {home / TOKEN_FILE}\n"
        "contexts:\n"
        "  - name: local\n    context:\n      cluster: local\n      user: enikk-runtime\n"
        "      namespace: enikk\n"
        "current-context: local\n",
    )
    return True


def _write_private(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch(mode=0o600, exist_ok=True)
    path.chmod(0o600)
    path.write_text(text, encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    """``enikk-kube PATH``: one read-only Kubernetes API call (GET), e.g.
    ``/api/v1/namespaces/enikk/pods`` or
    ``/api/v1/namespaces/enikk/pods/<pod>/log?tailLines=500&previous=true``.
    Prints the status line and the body; exits 1 on an error status."""
    parser = argparse.ArgumentParser(prog="enikk-kube", description=main.__doc__)
    parser.add_argument("path")
    args = parser.parse_args(argv)
    home = Path.home()
    if not (home / TOKEN_FILE).exists():
        print("no Kubernetes access in this session (ADR-0041 3c)", file=sys.stderr)
        return 2
    token = (home / TOKEN_FILE).read_text(encoding="utf-8").strip()
    server = (home / SERVER_FILE).read_text(encoding="utf-8").strip()
    response = httpx.get(
        server + "/" + args.path.lstrip("/"),
        headers={"Authorization": f"Bearer {token}"},
        verify=str(home / CA_FILE),
        timeout=TIMEOUT_SECONDS,
    )
    try:
        print(f"HTTP {response.status_code}")
        print(response.text)
    except BrokenPipeError:
        return 0
    return 0 if response.is_success else 1
