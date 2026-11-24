"""Cancelling a run ends the Claude Code CLI process (ADR-0037 3b)."""

from __future__ import annotations

import asyncio
import os
from typing import TYPE_CHECKING

import pytest
from claude_agent_sdk import ClaudeAgentOptions, query

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.anyio

FAKE_CLI = """#!/bin/sh
if [ "$1" = "-v" ] || [ "$1" = "--version" ]; then echo "9.9.9 (Claude Code)"; exit 0; fi
echo $$ > "{pidfile}"
exec cat > /dev/null
"""


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


async def test_a_cancelled_run_ends_the_cli_process(tmp_path: Path) -> None:
    pidfile = tmp_path / "pid"
    cli = tmp_path / "claude"
    cli.write_text(FAKE_CLI.format(pidfile=pidfile))
    cli.chmod(0o755)
    options = ClaudeAgentOptions(cli_path=cli, cwd=tmp_path)

    async def consume() -> None:
        async for _ in query(prompt="hi", options=options):
            pass

    run = asyncio.create_task(consume())
    for _ in range(200):
        if pidfile.exists() and pidfile.read_text().strip():
            break
        await asyncio.sleep(0.05)
    pid = int(pidfile.read_text())
    assert _alive(pid)

    run.cancel()
    with pytest.raises(asyncio.CancelledError):
        await run

    assert not _alive(pid)
