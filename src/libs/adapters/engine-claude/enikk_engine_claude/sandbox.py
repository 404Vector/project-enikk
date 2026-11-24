"""Keeps the engine's secrets away from the tools the model runs (ADR-0033).

Two small programs sit between the SDK and what the model can reach:

- ``enikk-claude-launch`` is the SDK's ``cli_path``. It moves the Claude token from
  its environment to a pipe (``CLAUDE_CODE_OAUTH_TOKEN_FILE_DESCRIPTOR``), so the CLI's
  environment holds no secret, restricts itself with Landlock to the allowed paths,
  checks that the paths that must stay secret cannot be read, and executes the CLI.
- ``enikk-sandbox-shell`` is ``CLAUDE_CODE_SHELL_PREFIX``: the CLI runs every shell
  command as ``enikk-sandbox-shell '<script>'``. It drops secret-looking variables
  and the token descriptor, enters a nested Landlock domain, checks the secret paths
  again, and runs the script with bash. A nested domain cannot read the memory or
  ``/proc/<pid>/environ`` of the CLI or the runtime (Landlock's ptrace scoping).

Landlock needs no privilege, only a kernel that has it; ``available()`` says whether
this one does. This module imports the standard library only, because the shell
prefix starts for every command; the secret pattern is read from the kernel's
redaction module file without importing the kernel package.
"""

from __future__ import annotations

import contextlib
import ctypes
import importlib.util
import os
import re
import sys
from pathlib import Path
from typing import Final

ENV_MODE: Final = "ENIKK_SANDBOX"
"""``landlock`` to restrict, ``off`` to pass the token on a pipe only."""
ENV_READ: Final = "ENIKK_SANDBOX_READ"
ENV_WRITE: Final = "ENIKK_SANDBOX_WRITE"
ENV_SECRETS: Final = "ENIKK_SANDBOX_SECRETS"
"""Paths that must not be readable inside the sandbox; checked after restricting."""
ENV_CLI: Final = "ENIKK_CLAUDE_CLI"
CONFIG_DIR: Final = "CLAUDE_CONFIG_DIR"
ENV_TOKEN: Final = "ENIKK_CLAUDE_OAUTH_TOKEN"  # noqa: S105 - variable name
TOKEN_FD: Final = "CLAUDE_CODE_OAUTH_TOKEN_FILE_DESCRIPTOR"  # noqa: S105 - variable name
LANDLOCK: Final = "landlock"
OFF: Final = "off"
SECRET_NAME: Final = re.compile(
    r"(TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL|API_KEY|PRIVATE_KEY|AUTH)", re.IGNORECASE
)
KEEP: Final = frozenset({ENV_MODE, ENV_READ, ENV_WRITE, ENV_SECRETS})
SHELLS: Final = ("/bin/bash", "/usr/bin/bash", "/bin/sh")

# Linux syscalls (the same numbers on x86_64 and arm64) and Landlock constants.
_SYS_CREATE_RULESET: Final = 444
_SYS_ADD_RULE: Final = 445
_SYS_RESTRICT_SELF: Final = 446
_VERSION_FLAG: Final = 1
_RULE_PATH_BENEATH: Final = 1
_PR_SET_NO_NEW_PRIVS: Final = 38
_EXECUTE, _WRITE_FILE, _READ_FILE, _READ_DIR = 1 << 0, 1 << 1, 1 << 2, 1 << 3
_TRUNCATE, _IOCTL_DEV = 1 << 14, 1 << 15
_FILE_RIGHTS: Final = _EXECUTE | _WRITE_FILE | _READ_FILE | _TRUNCATE | _IOCTL_DEV
"""The rights a rule on a single file (not a directory) may carry."""
_SCOPE_ABSTRACT_UNIX_SOCKET, _SCOPE_SIGNAL = 1 << 0, 1 << 1


class SandboxError(Exception):
    """The sandbox could not be set up; nothing is run without it."""


class _RulesetAttr(ctypes.Structure):
    _fields_ = (
        ("handled_access_fs", ctypes.c_uint64),
        ("handled_access_net", ctypes.c_uint64),
        ("scoped", ctypes.c_uint64),
    )


class _PathBeneath(ctypes.Structure):
    _pack_ = 1
    _fields_ = (
        ("allowed_access", ctypes.c_uint64),
        ("parent_fd", ctypes.c_int32),
    )


def _libc() -> ctypes.CDLL:
    return ctypes.CDLL(None, use_errno=True)


def abi() -> int:
    """The kernel's Landlock ABI version; 0 when it has no Landlock."""
    if not sys.platform.startswith("linux"):
        return 0
    version = _libc().syscall(_SYS_CREATE_RULESET, None, ctypes.c_size_t(0), _VERSION_FLAG)
    return max(int(version), 0)


def available() -> bool:
    return abi() >= 1


def _fs_rights(version: int) -> int:
    rights = (1 << 13) - 1  # ABI 1: execute .. make_sym
    if version >= 2:
        rights |= 1 << 13  # refer
    if version >= 3:
        rights |= _TRUNCATE
    if version >= 5:
        rights |= _IOCTL_DEV
    return rights


def restrict(read: list[Path], write: list[Path]) -> None:
    """Allow only reading (and executing) beneath ``read`` and anything beneath ``write``
    for this process and every process it starts. It cannot be undone."""
    version = abi()
    if version < 1:
        raise SandboxError("the kernel has no Landlock")
    libc = _libc()
    handled = _fs_rights(version)
    attr = _RulesetAttr(
        handled_access_fs=handled,
        handled_access_net=0,
        scoped=(_SCOPE_ABSTRACT_UNIX_SOCKET | _SCOPE_SIGNAL) if version >= 6 else 0,
    )
    size = ctypes.sizeof(attr) if version >= 6 else ctypes.sizeof(ctypes.c_uint64) * 2
    ruleset = libc.syscall(_SYS_CREATE_RULESET, ctypes.byref(attr), ctypes.c_size_t(size), 0)
    if ruleset < 0:
        raise SandboxError(f"landlock_create_ruleset: {os.strerror(ctypes.get_errno())}")
    try:
        for path in read:
            _allow(libc, ruleset, path, _EXECUTE | _READ_FILE | _READ_DIR)
        for path in write:
            _allow(libc, ruleset, path, handled)
        if libc.prctl(_PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0:
            raise SandboxError(f"prctl: {os.strerror(ctypes.get_errno())}")
        if libc.syscall(_SYS_RESTRICT_SELF, ruleset, 0) != 0:
            raise SandboxError(f"landlock_restrict_self: {os.strerror(ctypes.get_errno())}")
    finally:
        os.close(ruleset)


def _allow(libc: ctypes.CDLL, ruleset: int, path: Path, rights: int) -> None:
    try:
        fd = os.open(path, os.O_PATH | os.O_CLOEXEC)
    except FileNotFoundError:
        return
    try:
        if not path.is_dir():
            rights &= _FILE_RIGHTS
        beneath = _PathBeneath(allowed_access=rights, parent_fd=fd)
        if libc.syscall(_SYS_ADD_RULE, ruleset, _RULE_PATH_BENEATH, ctypes.byref(beneath), 0):
            raise SandboxError(f"landlock_add_rule {path}: {os.strerror(ctypes.get_errno())}")
    finally:
        os.close(fd)


def check_secrets_hidden(secrets: list[Path]) -> None:
    """Raise unless every existing path in ``secrets`` is unreadable here."""
    for secret in secrets:
        try:
            if secret.is_dir():
                os.listdir(secret)
            else:
                with secret.open("rb") as handle:
                    handle.read(1)
        except FileNotFoundError:
            continue
        except PermissionError:
            continue
        raise SandboxError(f"{secret} is still readable in the sandbox")


def _secret_pattern() -> re.Pattern[str]:
    """``enikk_kernel.redaction.SECRET``, loaded from its file: importing the kernel
    package would cost a third of a second on every command."""
    package = importlib.util.find_spec("enikk_kernel")
    folders = [] if package is None else list(package.submodule_search_locations or [])
    for folder in folders:
        spec = importlib.util.spec_from_file_location(
            "_enikk_redaction", Path(folder) / "redaction.py"
        )
        if spec is None or spec.loader is None:
            continue
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        pattern: re.Pattern[str] = module.SECRET
        return pattern
    raise SandboxError("enikk_kernel.redaction was not found")


def clean_environment(env: dict[str, str]) -> dict[str, str]:
    """``env`` without the token handover and anything that looks like a secret."""
    secret = _secret_pattern()
    return {
        name: value
        for name, value in env.items()
        if name in KEEP
        or (
            name not in (ENV_TOKEN, TOKEN_FD, ENV_CLI)
            and not SECRET_NAME.search(name)
            and not secret.search(value)
        )
    }


def with_config_dir(env: dict[str, str]) -> str:
    """The writable paths plus the run's ``CLAUDE_CONFIG_DIR``: its home, or the
    directory the SDK made to resume a session, which is only known here."""
    config_dir = env.get(CONFIG_DIR, "")
    paths = [p for p in env.get(ENV_WRITE, "").split(os.pathsep) if p]
    if config_dir and config_dir not in paths:
        paths.append(config_dir)
    return os.pathsep.join(paths)


def _paths(env: dict[str, str], name: str) -> list[Path]:
    return [Path(p) for p in env.get(name, "").split(os.pathsep) if p]


def _enter(env: dict[str, str]) -> None:
    if env.get(ENV_MODE) != LANDLOCK:
        return
    restrict(_paths(env, ENV_READ), _paths(env, ENV_WRITE))
    check_secrets_hidden(_paths(env, ENV_SECRETS))


def launch() -> None:
    """``cli_path``: hand the token over on a pipe, restrict, then become the CLI."""
    env = dict(os.environ)
    cli = env.pop(ENV_CLI, "")
    token = env.pop(ENV_TOKEN, "")
    env.pop("CLAUDE_CODE_OAUTH_TOKEN", None)
    if not cli:
        _die(f"{ENV_CLI} is not set")
    if token:
        read_end, write_end = os.pipe()
        os.write(write_end, token.encode())
        os.close(write_end)
        os.set_inheritable(read_end, True)
        env[TOKEN_FD] = str(read_end)
    env[ENV_WRITE] = with_config_dir(env)
    try:
        _enter(env)
    except SandboxError as exc:
        _die(f"refusing to start Claude Code: {exc}")
    os.execve(cli, [cli, *sys.argv[1:]], env)  # noqa: S606 - replaces this process


def shell() -> None:
    """``CLAUDE_CODE_SHELL_PREFIX``: run one shell script in a nested sandbox."""
    script = sys.argv[1] if len(sys.argv) > 1 else ""
    inherited = os.environ.get(TOKEN_FD, "")
    if inherited.isdigit():
        with contextlib.suppress(OSError):
            os.close(int(inherited))
    env = clean_environment(dict(os.environ))
    try:
        _enter(env)
    except SandboxError as exc:
        _die(f"refusing to run the command: {exc}")
    bash = next((s for s in SHELLS if Path(s).exists()), SHELLS[-1])
    os.execve(bash, [bash, "-c", script], env)  # noqa: S606 - replaces this process


def _die(message: str) -> None:
    sys.stderr.write(f"enikk sandbox: {message}\n")
    raise SystemExit(126)
