"""Post-deployment self check (ADR-0023 §2)."""

from __future__ import annotations

from typing import TYPE_CHECKING

import httpx
import pytest

from enikk_core.selfcheck import SelfCheck, SelfCheckError, SelfCheckSettings
from enikk_core_client import CoreClient

if TYPE_CHECKING:
    from pathlib import Path

    from conftest import Harness

    from enikk_core_client import TokenSource

pytestmark = pytest.mark.anyio


async def _no_sleep(seconds: float) -> None:
    return None


def _check(harness: Harness, tmp_path: Path, version: str, lines: list[str]) -> SelfCheck:
    token_file = tmp_path / "token"
    token_file.write_text("smoke\n")

    def connect(base_url: str, tokens: TokenSource) -> CoreClient:
        return CoreClient.connect(base_url, tokens, transport=httpx.ASGITransport(app=harness.app))

    settings = SelfCheckSettings(
        core_url="http://core.test", expected_version=version, token_file=token_file, attempts=3
    )
    return SelfCheck(settings, client=connect, report=lines.append, sleep=_no_sleep)


async def _served_version(harness: Harness) -> str:
    async with harness.client("smoke") as client:
        return (await client.health()).version


async def test_deployed_core_passes(harness: Harness, tmp_path: Path) -> None:
    lines: list[str] = []
    await _check(harness, tmp_path, await _served_version(harness), lines).run()
    assert any("forged token rejected" in line for line in lines)
    assert any("database" in line for line in lines)


async def test_old_version_still_serving_fails(harness: Harness, tmp_path: Path) -> None:
    with pytest.raises(SelfCheckError, match="does not serve sha-new"):
        await _check(harness, tmp_path, "sha-new", []).run()
