"""Schedules (ADR-0039): cron checks, tool limits and the approval rule."""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest
from pydantic import ValidationError

from enikk_contracts.agents import (
    AgentDefinition,
    McpToolGrant,
    PlatformTool,
    PromptRef,
    ToolPolicy,
)
from enikk_contracts.common import Engine
from enikk_contracts.schedules import (
    InputKind,
    OutputKind,
    ScheduleInput,
    ScheduleOutput,
    ScheduleState,
    Timing,
)
from enikk_core_domain.errors import InvalidRequestError, InvalidTransitionError
from enikk_core_domain.schedules import (
    Schedule,
    check_spec,
    may_manage,
    needs_approval,
    parse_cron,
    tools_beyond,
)
from enikk_core_domain.testing import T0, conversation, schedule_spec

ENIKK = AgentDefinition(
    name="enikk",
    aliases=("assistant",),
    description="enikk",
    engine=Engine.CLAUDE,
    prompt=PromptRef(name="enikk", label="production"),
    tools=ToolPolicy(
        builtin=("Read", "WebSearch"),
        mcp=(McpToolGrant(server="scheduler", allow=("list_schedules",)),),
    ),
)
OTHER = ENIKK.model_copy(update={"name": "other", "aliases": ()})


@pytest.mark.parametrize(
    ("cron", "gap"),
    [
        ("0 4 * * *", timedelta(days=1)),
        ("*/10 * * * *", timedelta(minutes=10)),
        ("*/5 9-17 * * 1-5", timedelta(minutes=5)),
        ("0 23,0 * * *", timedelta(hours=1)),
        ("50 23 * * *", timedelta(days=1)),
        ("0,30 8 1 1 *", timedelta(minutes=30)),
        ("5/20 * * * 7", timedelta(minutes=20)),
    ],
)
def test_shortest_gap(cron: str, gap: timedelta) -> None:
    assert parse_cron(cron).shortest_gap() == gap


def test_sunday_is_0_or_7() -> None:
    assert parse_cron("0 0 * * 7").weekdays == frozenset({0})


@pytest.mark.parametrize(
    "cron", ["0 4 * *", "60 * * * *", "0 24 * * *", "* * 0 * *", "*/0 * * * *", "5-1 * * * *"]
)
def test_invalid_cron(cron: str) -> None:
    with pytest.raises(InvalidRequestError):
        parse_cron(cron)


def test_timing_needs_exactly_one() -> None:
    with pytest.raises(ValidationError):
        Timing()
    with pytest.raises(ValidationError):
        Timing(cron="0 4 * * *", at=T0)


def test_inputs_and_outputs_are_registered_kinds() -> None:
    with pytest.raises(ValidationError):
        ScheduleInput(kind=InputKind.TEXT)
    with pytest.raises(ValidationError):
        ScheduleInput(kind=InputKind.DAY_CONVERSATIONS, text="also this")
    with pytest.raises(ValidationError):
        ScheduleOutput(kind=OutputKind.VAULT_NOTE)
    with pytest.raises(ValidationError):
        ScheduleOutput(kind=OutputKind.VAULT_NOTE, folder="../선호")
    with pytest.raises(ValidationError):
        ScheduleOutput(kind=OutputKind.OWNER_DM, folder="뉴스")
    with pytest.raises(ValidationError):
        ScheduleOutput.model_validate({"kind": "webhook"})


def test_tools_never_exceed_the_definition() -> None:
    within = ToolPolicy(builtin=("WebSearch",), platform_defaults=False)
    assert tools_beyond(within, ENIKK.tools) == []
    wide = ToolPolicy(
        builtin=("Bash",),
        mcp=(McpToolGrant(server="scheduler", allow=("list_schedules", "delete_schedule")),),
        platform=(PlatformTool.GITHUB,),
    )
    assert tools_beyond(wide, ENIKK.tools) == [
        "builtin:Bash",
        "platform:github",
        "mcp:scheduler/delete_schedule",
    ]
    narrow = ENIKK.model_copy(update={"tools": ToolPolicy(platform_defaults=False)})
    assert tools_beyond(ToolPolicy(), narrow.tools) == ["platform:defaults"]


def test_check_spec() -> None:
    check_spec(schedule_spec(), ENIKK)
    with pytest.raises(InvalidRequestError, match="no definition"):
        check_spec(schedule_spec(agent="ghost"), None)
    wide = schedule_spec().model_copy(update={"tools": ToolPolicy(builtin=("Bash",))})
    with pytest.raises(InvalidRequestError, match="I-16"):
        check_spec(wide, ENIKK)
    zone = schedule_spec().model_copy(update={"timing": Timing(cron="0 4 * * *", timezone="X/Y")})
    with pytest.raises(InvalidRequestError, match="time zone"):
        check_spec(zone, ENIKK)
    with pytest.raises(InvalidRequestError, match="cron"):
        check_spec(schedule_spec(cron="0 25 * * *"), ENIKK)


def test_own_range_applies_at_once() -> None:
    assert not needs_approval(schedule_spec(), ENIKK, "enikk")
    assert not needs_approval(schedule_spec(), ENIKK, "assistant")
    assert not needs_approval(schedule_spec(cron="*/10 * * * *"), ENIKK, "enikk")
    once = schedule_spec().model_copy(update={"timing": Timing(at=T0)})
    assert not needs_approval(once, ENIKK, "enikk")


def test_other_agents_and_frequent_runs_need_approval() -> None:
    assert needs_approval(schedule_spec(agent="other"), OTHER, "enikk")
    assert needs_approval(schedule_spec(cron="*/9 * * * *"), ENIKK, "enikk")


def test_an_agent_manages_itself_its_delegates_and_agents_it_covers() -> None:
    """ADR-0052: no agent reaches tools it does not have through a schedule."""
    painter = ENIKK.model_copy(
        update={
            "name": "painter",
            "aliases": (),
            "tools": ToolPolicy(mcp=(McpToolGrant(server="imagegen", allow=("generate",)),)),
        }
    )
    writer = ENIKK.model_copy(
        update={"name": "writer", "aliases": (), "tools": ToolPolicy(builtin=("Read",))}
    )
    quiet = ENIKK.model_copy(
        update={"name": "quiet", "aliases": (), "tools": ToolPolicy(platform_defaults=False)}
    )
    boss = ENIKK.model_copy(update={"delegates": ("painter",)})
    assert may_manage(painter, painter)
    assert not may_manage(painter, ENIKK)
    assert not may_manage(painter, writer)
    assert may_manage(boss, painter)
    assert not may_manage(ENIKK, painter)
    assert may_manage(ENIKK, writer)
    assert may_manage(painter, quiet)
    assert not may_manage(quiet, painter)
    vault = writer.model_copy(update={"tools": ToolPolicy(platform=(PlatformTool.VAULT_PRIVATE,))})
    assert not may_manage(ENIKK, vault)


def test_unknown_callers_manage_nothing_and_orphans_are_anyones() -> None:
    assert not may_manage(None, ENIKK)
    assert may_manage(OTHER, None)


def _schedule(*, approval: bool) -> Schedule:
    return Schedule.create(
        schedule_id=uuid.uuid4(),
        name="news",
        spec=schedule_spec(),
        owner_agent="enikk",
        origin=conversation("1.0"),
        approval=approval,
        now=T0,
    )


def test_states() -> None:
    later = T0 + timedelta(minutes=1)
    pending = _schedule(approval=True)
    assert pending.state is ScheduleState.PENDING
    with pytest.raises(InvalidTransitionError):
        pending.pause(later)
    with pytest.raises(InvalidTransitionError):
        pending.resume(later)
    active = _schedule(approval=False)
    paused = active.pause(later)
    assert paused.state is ScheduleState.PAUSED
    assert paused.replace(schedule_spec(), approval=False, now=later).state is ScheduleState.PAUSED
    assert paused.replace(schedule_spec(), approval=True, now=later).state is ScheduleState.PENDING
    assert paused.resume(later).state is ScheduleState.ACTIVE
    assert active.view().name == "news"
