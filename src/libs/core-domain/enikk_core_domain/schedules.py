"""Schedules (ADR-0039): the record, its checks and the rule for the owner's approval.

A schedule's tools never exceed its agent's definition (I-16). A change inside the
calling agent's own range applies at once; any other waits for the owner (ADR-0039 §5).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from itertools import pairwise
from typing import TYPE_CHECKING, Annotated, Final, Self
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import AwareDatetime, Field

from enikk_contracts.common import AgentName, ConversationRef
from enikk_contracts.schedules import (
    RunStatus,
    RunView,
    ScheduleName,
    ScheduleSpec,
    ScheduleState,
    ScheduleView,
)
from enikk_core_domain.errors import InvalidRequestError, InvalidTransitionError
from enikk_core_domain.model import Entity
from enikk_kernel.models import FrozenModel

if TYPE_CHECKING:
    from enikk_contracts.agents import AgentDefinition, ToolPolicy

MIN_INTERVAL: Final = timedelta(minutes=10)
"""Schedules that may run more often than this need the owner's approval."""
_DAY: Final = 24 * 60
_FIELDS: Final = (("minute", 0, 59), ("hour", 0, 23), ("day", 1, 31), ("month", 1, 12))


class CronFields(FrozenModel):
    """A parsed 5-field cron expression: the values each field allows."""

    minutes: frozenset[int]
    hours: frozenset[int]
    days: frozenset[int]
    months: frozenset[int]
    weekdays: frozenset[int]
    """0 is Sunday; 7 is read as 0."""

    def shortest_gap(self) -> timedelta:
        """A lower bound of the time between two runs.

        Runs within one day are compared exactly. Across midnight the next day is assumed
        allowed, so the bound may be shorter than the real gap, never longer.
        """
        times = sorted(h * 60 + m for h in self.hours for m in self.minutes)
        gaps = [b - a for a, b in pairwise(times)]
        gaps.append(times[0] + _DAY - times[-1])
        return timedelta(minutes=min(gaps))


def parse_cron(expression: str) -> CronFields:
    parts = expression.split()
    if len(parts) != 5:
        raise InvalidRequestError("a cron expression has 5 fields: minute hour day month weekday")
    values = [
        _parse_field(part, name, low, high)
        for part, (name, low, high) in zip(parts, _FIELDS, strict=False)
    ]
    weekdays = frozenset(d % 7 for d in _parse_field(parts[4], "weekday", 0, 7))
    return CronFields(
        minutes=values[0], hours=values[1], days=values[2], months=values[3], weekdays=weekdays
    )


def _parse_field(text: str, name: str, low: int, high: int) -> frozenset[int]:
    allowed: set[int] = set()
    for item in text.split(","):
        span, _, step_text = item.partition("/")
        step = _number(step_text, name) if step_text else 1
        if span == "*":
            start, end = low, high
        elif "-" in span:
            first, _, last = span.partition("-")
            start, end = _number(first, name), _number(last, name)
        else:
            start = _number(span, name)
            end = high if step_text else start
        if step < 1 or not low <= start <= end <= high:
            raise InvalidRequestError(f"cron {name} '{item}' is outside {low}-{high}")
        allowed.update(range(start, end + 1, step))
    return frozenset(allowed)


def _number(text: str, name: str) -> int:
    if not text.isdigit():
        raise InvalidRequestError(f"cron {name} has '{text}', not a number")
    return int(text)


def check_spec(spec: ScheduleSpec, definition: AgentDefinition | None) -> AgentDefinition:
    """Return the definition of ``spec``'s agent, or raise if ``spec`` cannot be stored at
    all. No approval can widen these (I-16)."""
    if definition is None:
        raise InvalidRequestError(f"agent '{spec.agent}' has no definition")
    try:
        ZoneInfo(spec.timing.timezone)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise InvalidRequestError(f"unknown time zone '{spec.timing.timezone}'") from exc
    if spec.timing.cron is not None:
        parse_cron(spec.timing.cron)
    beyond = tools_beyond(spec.tools, definition.tools)
    if beyond:
        raise InvalidRequestError(
            f"tools {beyond} are not in the definition of '{definition.name}' (I-16)"
        )
    return definition


def tools_beyond(wanted: ToolPolicy, granted: ToolPolicy) -> list[str]:
    """The tools of ``wanted`` that ``granted`` does not give, by name."""
    beyond = [f"builtin:{t}" for t in wanted.builtin if t not in granted.builtin]
    beyond += [f"platform:{t}" for t in wanted.platform if t not in granted.platform]
    if wanted.platform_defaults and not granted.platform_defaults:
        beyond.append("platform:defaults")
    servers = {grant.server: set(grant.allow) for grant in granted.mcp}
    for grant in wanted.mcp:
        allowed = servers.get(grant.server, set())
        beyond += [f"mcp:{grant.server}/{t}" for t in grant.allow if t not in allowed]
    return beyond


def may_manage(caller: AgentDefinition | None, target: AgentDefinition | None) -> bool:
    """Whether the calling agent may create, change, run or see schedules of ``target``
    (ADR-0052).

    An agent manages its own schedules, those of the agents it delegates to, and those of
    any agent whose tools it has itself. A schedule whose agent has no definition any
    more can do nothing, so any known caller may manage it.
    """
    if caller is None:
        return False
    if target is None:
        return True
    if caller.name == target.name or target.name in caller.delegates:
        return True
    return not tools_beyond(target.tools, caller.tools)


def needs_approval(spec: ScheduleSpec, target: AgentDefinition, caller_agent: str) -> bool:
    """Whether the change is outside the caller's own range (ADR-0039 §5).

    ``target`` is the definition of the schedule's agent and ``caller_agent`` the agent
    of the calling task, possibly under a former name (an alias of ``target``).
    """
    own = caller_agent == target.name or caller_agent in target.aliases
    if not own:
        return True
    if spec.timing.cron is None:
        return False
    return parse_cron(spec.timing.cron).shortest_gap() < MIN_INTERVAL


class Schedule(Entity):
    id: uuid.UUID
    name: ScheduleName
    spec: ScheduleSpec
    state: ScheduleState
    owner_agent: AgentName
    origin: ConversationRef
    created_at: AwareDatetime
    updated_at: AwareDatetime

    @classmethod
    def create(
        cls,
        *,
        schedule_id: uuid.UUID,
        name: str,
        spec: ScheduleSpec,
        owner_agent: str,
        origin: ConversationRef,
        approval: bool,
        now: datetime,
    ) -> Self:
        state = ScheduleState.PENDING if approval else ScheduleState.ACTIVE
        return cls(
            id=schedule_id,
            name=name,
            spec=spec,
            state=state,
            owner_agent=owner_agent,
            origin=origin,
            created_at=now,
            updated_at=now,
        )

    def replace(self, spec: ScheduleSpec, *, approval: bool, now: datetime) -> Self:
        """A new spec. It waits for approval if needed; otherwise a paused one stays paused."""
        if approval:
            state = ScheduleState.PENDING
        elif self.state is ScheduleState.PAUSED:
            state = ScheduleState.PAUSED
        else:
            state = ScheduleState.ACTIVE
        return self._evolve(spec=spec, state=state, updated_at=now)

    def pause(self, now: datetime) -> Self:
        if self.state is ScheduleState.PENDING:
            raise InvalidTransitionError(f"schedule {self.name} waits for approval")
        return self._evolve(state=ScheduleState.PAUSED, updated_at=now)

    def resume(self, now: datetime) -> Self:
        if self.state is ScheduleState.PENDING:
            raise InvalidTransitionError(f"schedule {self.name} waits for approval")
        return self._evolve(state=ScheduleState.ACTIVE, updated_at=now)

    def view(self) -> ScheduleView:
        return ScheduleView(**dict(self))


class ScheduleRun(Entity):
    """One run of a schedule, for one due time (``slot``). At most one per slot."""

    schedule_id: uuid.UUID
    slot: AwareDatetime
    status: RunStatus
    task_id: uuid.UUID | None = None
    attempts: Annotated[int, Field(ge=0)] = 0
    detail: Annotated[str, Field(max_length=500)] = ""
    started_at: AwareDatetime
    updated_at: AwareDatetime

    def view(self) -> RunView:
        return RunView(**dict(self))

    def retried(self, task_id: uuid.UUID, now: datetime) -> Self:
        return self._evolve(task_id=task_id, attempts=self.attempts + 1, updated_at=now)

    def ended(self, status: RunStatus, detail: str, now: datetime) -> Self:
        return self._evolve(status=status, detail=detail[:500], updated_at=now)


MAX_DUE: Final = 15
"""At most this many due times are made at once: the newest and up to 14 missed."""


def due_slots(spec: ScheduleSpec, after: datetime, until: datetime) -> list[datetime]:
    """The due times in ``(after, until]``, oldest first, at most ``catchup + 1`` (the
    newest ones). Cron times are read in the spec's time zone (ADR-0039 S5)."""
    timing = spec.timing
    keep = spec.catchup + 1
    if timing.at is not None:
        return [timing.at] if after < timing.at <= until else []
    if timing.cron is None:
        return []
    fields = parse_cron(timing.cron)
    zone = ZoneInfo(timing.timezone)
    restricted_day = fields.days != frozenset(range(1, 32))
    restricted_weekday = fields.weekdays != frozenset(range(7))
    found: list[datetime] = []
    day = until.astimezone(zone).date()
    first = max(after, until - timedelta(days=MAX_DUE)).astimezone(zone).date()
    while day >= first and len(found) < keep:
        weekday = (day.weekday() + 1) % 7
        by_day = day.day in fields.days
        by_weekday = weekday in fields.weekdays
        matches = (
            (by_day or by_weekday)
            if restricted_day and restricted_weekday
            else (by_day and by_weekday)
        )
        if matches and day.month in fields.months:
            for hour in sorted(fields.hours, reverse=True):
                for minute in sorted(fields.minutes, reverse=True):
                    local = datetime(day.year, day.month, day.day, hour, minute, tzinfo=zone)
                    slot = local.astimezone(UTC)
                    if after < slot <= until and len(found) < keep:
                        found.append(slot)
        day -= timedelta(days=1)
    return sorted(found)


def started_run(
    schedule_id: uuid.UUID, slot: datetime, task_id: uuid.UUID | None, now: datetime
) -> ScheduleRun:
    return ScheduleRun(
        schedule_id=schedule_id,
        slot=slot,
        status=RunStatus.RUNNING,
        task_id=task_id,
        attempts=1,
        started_at=now,
        updated_at=now,
    )
