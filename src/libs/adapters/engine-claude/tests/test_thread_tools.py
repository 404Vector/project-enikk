"""``send_file`` sends only the session's own files; the Slack tools (ADR-0042)."""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any

import pytest
from pydantic import HttpUrl

from enikk_contracts.thread_actions import (
    LinkTarget,
    MessageLink,
    MessageLinkRequest,
    ScheduledMessage,
    ScheduleMessageRequest,
    ScheduleTarget,
)
from enikk_engine_claude.current import CurrentJob
from enikk_engine_claude.platform_tools import (
    THREAD_TOOLS,
    granted_platform_tools,
    sendable_file,
    slack_tools,
)
from enikk_runtime.model import EngineJob, ResolvedPrompt, SessionWorkspace
from enikk_runtime.testing import definition

if TYPE_CHECKING:
    from pathlib import Path

    from claude_agent_sdk import SdkMcpTool


def _job(tmp_path: Path, *, workspace: bool = True) -> EngineJob:
    session = "00000000-0000-0000-0000-000000000001"
    return EngineJob(
        task_id=uuid.uuid4(),
        lease_id=uuid.uuid4(),
        attempt=1,
        session_id=uuid.uuid4(),
        engine_session_id=None,
        definition=definition(),
        prompt=ResolvedPrompt(name="enikk", version=1, text="hi", model="claude-test-1"),
        input="send me the icons",
        workspace=(
            SessionWorkspace(
                repository="404Vector/project-enikk",
                repo_dir=tmp_path / "repo.git",
                snapshot_dir=None,
                commit=None,
                fetched_at=None,
                fresh=True,
                worktree_dir=tmp_path / "worktrees" / session,
                branch=None,
                tmp_dir=tmp_path / "tmp" / session,
            )
            if workspace
            else None
        ),
    )


def test_a_file_in_tmp_or_the_worktree_can_be_sent(tmp_path: Path) -> None:
    job = _job(tmp_path)
    for root in (tmp_path / "tmp", tmp_path / "worktrees"):
        own = root / "00000000-0000-0000-0000-000000000001" / "png" / "e-done.png"
        own.parent.mkdir(parents=True)
        own.write_bytes(b"png")
        assert sendable_file(job, str(own)) == own.resolve()


def test_files_elsewhere_are_refused(tmp_path: Path) -> None:
    job = _job(tmp_path)
    other = tmp_path / "tmp" / "another-session" / "secret.txt"
    other.parent.mkdir(parents=True)
    other.write_text("x")
    (tmp_path / "tmp" / "00000000-0000-0000-0000-000000000001").mkdir(parents=True)
    escape = tmp_path / "tmp" / "00000000-0000-0000-0000-000000000001" / ".." / "another-session"

    assert isinstance(sendable_file(job, str(other)), str)
    assert isinstance(sendable_file(job, str(escape / "secret.txt")), str)
    assert isinstance(sendable_file(job, str(tmp_path / "missing")), str)


def test_a_link_out_of_the_workspace_is_refused(tmp_path: Path) -> None:
    job = _job(tmp_path)
    outside = tmp_path / "outside.txt"
    outside.write_text("x")
    link = tmp_path / "tmp" / "00000000-0000-0000-0000-000000000001" / "link.txt"
    link.parent.mkdir(parents=True)
    link.symlink_to(outside)

    assert isinstance(sendable_file(job, str(link)), str)


def test_empty_files_and_no_workspace_are_refused(tmp_path: Path) -> None:
    job = _job(tmp_path)
    empty = tmp_path / "tmp" / "00000000-0000-0000-0000-000000000001" / "empty"
    empty.parent.mkdir(parents=True)
    empty.write_bytes(b"")

    assert isinstance(sendable_file(job, str(empty)), str)
    assert isinstance(sendable_file(_job(tmp_path, workspace=False), str(empty)), str)


def test_thread_tools_follow_the_platform_defaults(tmp_path: Path) -> None:
    job = _job(tmp_path)
    assert set(THREAD_TOOLS) <= set(granted_platform_tools(job, thread=True))
    assert not set(THREAD_TOOLS) & set(granted_platform_tools(job, thread=False))


class _Poster:
    """Records what the Slack tools ask core (ADR-0042 stage 2)."""

    def __init__(self) -> None:
        self.scheduled: list[ScheduleMessageRequest] = []
        self.links: list[MessageLinkRequest] = []

    async def message_link(self, task_id: uuid.UUID, request: MessageLinkRequest) -> MessageLink:
        self.links.append(request)
        return MessageLink(url=HttpUrl("https://enikk.slack.com/archives/D1/p18001"))

    async def schedule_message(
        self, task_id: uuid.UUID, request: ScheduleMessageRequest
    ) -> ScheduledMessage:
        self.scheduled.append(request)
        return ScheduledMessage(
            message_id=uuid.uuid4(), at=request.at, target=request.target, text=request.text
        )


def _tool(tmp_path: Path, poster: _Poster, name: str) -> SdkMcpTool[Any]:
    tools = slack_tools(CurrentJob(_job(tmp_path)), poster)  # type: ignore[arg-type]
    return next(t for t in tools if t.name == name)


def test_every_slack_tool_is_granted_with_the_thread_tools() -> None:
    names = {t.rsplit("__", 1)[1] for t in THREAD_TOOLS}
    assert {"message_link", "list_emoji", "schedule_message", "cancel_scheduled"} <= names


@pytest.mark.anyio
async def test_schedule_message_reads_an_iso_time_and_the_target(tmp_path: Path) -> None:
    poster = _Poster()
    tool = _tool(tmp_path, poster, "schedule_message")

    done = await tool.handler({"text": "회의", "at": "2026-11-26T09:00:00+09:00", "target": "dm"})
    naive = await tool.handler({"text": "회의", "at": "2026-11-26T09:00:00"})

    assert not done["is_error"]
    [request] = poster.scheduled
    assert (request.at.isoformat(), request.target) == (
        "2026-11-26T09:00:00+09:00",
        ScheduleTarget.DM,
    )
    assert naive["is_error"]


@pytest.mark.anyio
async def test_message_link_needs_a_known_target(tmp_path: Path) -> None:
    poster = _Poster()
    tool = _tool(tmp_path, poster, "message_link")

    found = await tool.handler({"target": "request"})
    wrong = await tool.handler({"target": "somewhere"})

    assert "archives/D1/p18001" in found["content"][0]["text"]
    assert [r.target for r in poster.links] == [LinkTarget.REQUEST]
    assert wrong["is_error"]
