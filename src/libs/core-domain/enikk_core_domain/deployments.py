"""Judging deployments from Argo CD Application state (ADR-0023 §1).

Pure logic: core's deployment watcher feeds it what Argo CD shows and stores the
result. A rollout is one Argo CD sync operation of one app; the rollouts of all apps
that deployed the same commit form one deployment, reported to the owner once.
"""

from __future__ import annotations

import re
from datetime import timedelta
from enum import StrEnum
from typing import Annotated, Final

from pydantic import AwareDatetime, Field

from enikk_kernel.models import FrozenModel

IMAGE_TAG: Final = re.compile(r"^sha-([0-9a-f]{40})$")
COMMIT: Final = re.compile(r"^[0-9a-f]{40}$")
FAILED_PHASES: Final = frozenset({"Failed", "Error"})
MAX_SEEN_RUNS: Final = 200
MAX_REVERTS: Final = 50
MAX_REPORTED: Final = 50
MAX_DETAIL: Final = 500


class AppObservation(FrozenModel):
    """What Argo CD shows for one Application now."""

    app: str
    image_tag: str | None
    operation_started_at: AwareDatetime | None
    operation_phase: str | None
    operation_revision: str | None
    operation_message: str = ""
    health: str = "Unknown"


class RolloutOutcome(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    REPLACED = "replaced"
    """A newer sync of the same app started before this one settled."""


class AppRollout(FrozenModel):
    app: str
    operation_started_at: AwareDatetime
    observed_at: AwareDatetime
    outcome: RolloutOutcome | None = None
    detail: Annotated[str, Field(max_length=MAX_DETAIL)] = ""


class PendingDeployment(FrozenModel):
    commit: Annotated[str, Field(pattern=COMMIT.pattern)]
    rollouts: tuple[AppRollout, ...]
    last_change_at: AwareDatetime

    @property
    def settled(self) -> bool:
        return all(r.outcome is not None for r in self.rollouts)


class AppBaseline(FrozenModel):
    app: str
    image_tag: str | None
    operation_started_at: AwareDatetime | None


class FinishedDeployment(FrozenModel):
    commit: str
    rollouts: tuple[AppRollout, ...]

    @property
    def succeeded(self) -> bool:
        return not any(r.outcome is RolloutOutcome.FAILED for r in self.rollouts)

    @property
    def worth_reporting(self) -> bool:
        """Every app was replaced by a newer sync: the newer deployment reports instead."""
        return any(r.outcome is not RolloutOutcome.REPLACED for r in self.rollouts)


class RevertRecord(FrozenModel):
    """A revert PR core opened for a failed deployment or build (ADR-0023 §5)."""

    commit: Annotated[str, Field(pattern=COMMIT.pattern)]
    """The commit being reverted."""
    pull_number: Annotated[int, Field(ge=1)]
    pull_url: str
    requested_at: AwareDatetime


class WatchTiming(FrozenModel):
    timeout: timedelta
    """A rollout that has not succeeded by then has failed."""
    settle: timedelta
    """Quiet time after the last app joined before a deployment is reported."""


class DeploymentWatch(FrozenModel):
    """The watcher's memory, stored by core between ticks and across restarts."""

    baselines: tuple[AppBaseline, ...] = ()
    pending: tuple[PendingDeployment, ...] = ()
    seen_build_runs: tuple[int, ...] = ()
    builds_since: AwareDatetime | None = None
    """Build runs created before the watcher first ran are history, not news."""
    reverts: tuple[RevertRecord, ...] = ()
    reported: tuple[str, ...] = ()
    """Commits already reported. Another rollout of one of them (an app updated after the
    report, or a second sync of the same commit) is reported only if it fails."""

    def observe(
        self, observations: list[AppObservation], now: AwareDatetime, timing: WatchTiming
    ) -> tuple[DeploymentWatch, list[FinishedDeployment]]:
        baselines = {b.app: b for b in self.baselines}
        pending = {p.commit: p for p in self.pending}
        current = {o.app: o for o in observations}

        for obs in observations:
            base = baselines.get(obs.app)
            baselines[obs.app] = AppBaseline(
                app=obs.app,
                image_tag=obs.image_tag,
                operation_started_at=obs.operation_started_at,
            )
            started = obs.operation_started_at
            if base is None or started is None or started == base.operation_started_at:
                continue
            commit = _commit(obs, base)
            if commit is None:
                continue
            pending = _replace_older(pending, obs.app, now)
            rollout = AppRollout(app=obs.app, operation_started_at=started, observed_at=now)
            deployment = pending.get(commit)
            rollouts = (*deployment.rollouts, rollout) if deployment else (rollout,)
            pending[commit] = PendingDeployment(
                commit=commit, rollouts=rollouts, last_change_at=now
            )

        finished: list[FinishedDeployment] = []
        remaining: list[PendingDeployment] = []
        reported = list(self.reported)
        for deployment in pending.values():
            judged = deployment.model_copy(
                update={
                    "rollouts": tuple(
                        _judge(r, current.get(r.app), now, timing) for r in deployment.rollouts
                    )
                }
            )
            if judged.settled and now - judged.last_change_at >= timing.settle:
                result = FinishedDeployment(commit=judged.commit, rollouts=judged.rollouts)
                again = result.commit in reported
                if result.worth_reporting and (not again or not result.succeeded):
                    finished.append(result)
                if not again:
                    reported.append(result.commit)
            else:
                remaining.append(judged)

        state = self.model_copy(
            update={
                "baselines": tuple(sorted(baselines.values(), key=lambda b: b.app)),
                "pending": tuple(remaining),
                "builds_since": self.builds_since or now,
                "reported": tuple(reported[-MAX_REPORTED:]),
            }
        )
        return state, finished

    def revert_of(self, commit: str) -> RevertRecord | None:
        return next((r for r in self.reverts if r.commit == commit), None)

    def rollback_target(self, pull_number: int) -> str | None:
        """The commit a merged revert PR reverted, if ``pull_number`` is one of ours."""
        return next((r.commit for r in self.reverts if r.pull_number == pull_number), None)

    def with_revert(self, record: RevertRecord) -> DeploymentWatch:
        kept = tuple(r for r in self.reverts if r.commit != record.commit)
        return self.model_copy(update={"reverts": (*kept, record)[-MAX_REVERTS:]})

    def is_new_build_run(self, run_id: int, created_at: AwareDatetime) -> bool:
        since = self.builds_since
        return run_id not in self.seen_build_runs and (since is None or created_at >= since)

    def saw_build_runs(self, run_ids: list[int]) -> DeploymentWatch:
        seen = (*self.seen_build_runs, *(r for r in run_ids if r not in self.seen_build_runs))
        return self.model_copy(update={"seen_build_runs": seen[-MAX_SEEN_RUNS:]})


def _commit(obs: AppObservation, base: AppBaseline) -> str | None:
    """The commit a sync deployed: the image's, or the chart revision's if the image stayed."""
    if obs.image_tag != base.image_tag and obs.image_tag is not None:
        match = IMAGE_TAG.match(obs.image_tag)
        if match:
            return match.group(1)
    revision = obs.operation_revision
    if revision is not None and COMMIT.match(revision):
        return revision
    return None


def _replace_older(
    pending: dict[str, PendingDeployment], app: str, now: AwareDatetime
) -> dict[str, PendingDeployment]:
    updated: dict[str, PendingDeployment] = {}
    for commit, deployment in pending.items():
        rollouts = tuple(
            r.model_copy(update={"outcome": RolloutOutcome.REPLACED})
            if r.app == app and r.outcome is None
            else r
            for r in deployment.rollouts
        )
        changed = rollouts != deployment.rollouts
        updated[commit] = deployment.model_copy(
            update={"rollouts": rollouts, **({"last_change_at": now} if changed else {})}
        )
    return updated


def _judge(
    rollout: AppRollout, obs: AppObservation | None, now: AwareDatetime, timing: WatchTiming
) -> AppRollout:
    if rollout.outcome is not None:
        return rollout
    if obs is None:
        return _decided(rollout, RolloutOutcome.FAILED, "the Argo CD Application disappeared")
    if obs.operation_started_at != rollout.operation_started_at:
        return _decided(rollout, RolloutOutcome.REPLACED, "")
    phase = obs.operation_phase
    if phase in FAILED_PHASES:
        return _decided(rollout, RolloutOutcome.FAILED, obs.operation_message or f"sync {phase}")
    if phase == "Succeeded" and obs.health == "Healthy":
        return _decided(rollout, RolloutOutcome.SUCCEEDED, "")
    if phase == "Succeeded" and obs.health == "Degraded":
        return _decided(rollout, RolloutOutcome.FAILED, "the app is Degraded")
    if now - rollout.observed_at >= timing.timeout:
        minutes = int(timing.timeout.total_seconds() // 60)
        detail = f"not healthy within {minutes} minutes (sync {phase}, health {obs.health})"
        return _decided(rollout, RolloutOutcome.FAILED, detail)
    return rollout


def _decided(rollout: AppRollout, outcome: RolloutOutcome, detail: str) -> AppRollout:
    return rollout.model_copy(update={"outcome": outcome, "detail": detail[:MAX_DETAIL]})
