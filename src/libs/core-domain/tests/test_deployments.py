"""Judging deployments from Argo CD state (ADR-0023 §1)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from enikk_core_domain.deployments import (
    AppObservation,
    DeploymentWatch,
    RolloutOutcome,
    WatchTiming,
)

T0 = datetime(2026, 11, 22, 9, 0, tzinfo=UTC)
OLD = "a" * 40
NEW = "b" * 40
CHART = "c" * 40
TIMING = WatchTiming(timeout=timedelta(minutes=10), settle=timedelta(seconds=60))
APPS = ("enikk-core", "enikk-runtime-claude")


def _obs(
    app: str,
    commit: str,
    started: datetime | None,
    phase: str | None = "Succeeded",
    health: str = "Healthy",
    revision: str = CHART,
    message: str = "",
) -> AppObservation:
    return AppObservation(
        app=app,
        image_tag=f"sha-{commit}",
        operation_started_at=started,
        operation_phase=phase,
        operation_revision=revision,
        operation_message=message,
        health=health,
    )


def _baseline() -> DeploymentWatch:
    watch, finished = DeploymentWatch().observe([_obs(app, OLD, T0) for app in APPS], T0, TIMING)
    assert finished == []
    return watch


def _at(seconds: int) -> datetime:
    return T0 + timedelta(seconds=seconds)


def test_first_observation_is_only_a_baseline() -> None:
    watch = _baseline()
    assert watch.pending == ()
    assert {b.app for b in watch.baselines} == set(APPS)
    assert watch.builds_since == T0


def test_apps_of_one_commit_are_reported_together_once_settled() -> None:
    watch = _baseline()
    start = _at(100)
    running = [_obs(app, NEW, start, phase="Running", health="Progressing") for app in APPS]
    watch, finished = watch.observe(running, _at(100), TIMING)
    assert finished == []
    [pending] = watch.pending
    assert pending.commit == NEW
    assert [r.app for r in pending.rollouts] == list(APPS)

    done = [_obs(app, NEW, start) for app in APPS]
    watch, finished = watch.observe(done, _at(130), TIMING)
    assert finished == []  # still inside the settle window

    watch, finished = watch.observe(done, _at(161), TIMING)
    [deployment] = finished
    assert deployment.commit == NEW
    assert deployment.succeeded
    assert {r.outcome for r in deployment.rollouts} == {RolloutOutcome.SUCCEEDED}
    assert watch.pending == ()

    watch, finished = watch.observe(done, _at(400), TIMING)
    assert finished == []


def test_failed_sync_is_a_failed_deployment() -> None:
    watch = _baseline()
    start = _at(100)
    failing = [
        _obs("enikk-core", NEW, start, phase="Failed", message="PostSync hook failed"),
        _obs("enikk-runtime-claude", NEW, start),
    ]
    watch, _ = watch.observe(failing, _at(100), TIMING)
    _, finished = watch.observe(failing, _at(200), TIMING)
    [deployment] = finished
    assert not deployment.succeeded
    core = next(r for r in deployment.rollouts if r.app == "enikk-core")
    assert core.outcome is RolloutOutcome.FAILED
    assert core.detail == "PostSync hook failed"


def test_a_rollout_that_never_becomes_healthy_times_out() -> None:
    watch = _baseline()
    start = _at(100)
    stuck = [_obs("enikk-core", NEW, start, phase="Running", health="Progressing")]
    watch, _ = watch.observe(stuck, _at(100), TIMING)
    watch, finished = watch.observe(stuck, _at(100 + 599), TIMING)
    assert finished == []
    _, finished = watch.observe(stuck, _at(100 + 600), TIMING)
    [deployment] = finished
    assert not deployment.succeeded
    assert "not healthy within 10 minutes" in deployment.rollouts[0].detail


def test_chart_only_change_is_attributed_to_the_sync_revision() -> None:
    watch = _baseline()
    start = _at(100)
    synced = [_obs("enikk-core", OLD, start, revision=NEW)]
    watch, _ = watch.observe(synced, _at(100), TIMING)
    [pending] = watch.pending
    assert pending.commit == NEW


def test_a_newer_sync_replaces_the_unsettled_one() -> None:
    watch = _baseline()
    first = [_obs("enikk-core", NEW, _at(100), phase="Running", health="Progressing")]
    watch, _ = watch.observe(first, _at(100), TIMING)
    newer = "d" * 40
    second = [_obs("enikk-core", newer, _at(150))]
    watch, _ = watch.observe(second, _at(150), TIMING)
    _, finished = watch.observe(second, _at(300), TIMING)
    assert [d.commit for d in finished] == [newer]


def test_build_runs_are_reported_once_and_only_after_the_watcher_started() -> None:
    watch = _baseline()
    assert not watch.is_new_build_run(1, T0 - timedelta(minutes=1))
    assert watch.is_new_build_run(2, _at(10))
    watch = watch.saw_build_runs([2])
    assert not watch.is_new_build_run(2, _at(10))
