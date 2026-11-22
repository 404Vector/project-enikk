"""Deployment and build notices for the owner's DM (ADR-0023)."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import TYPE_CHECKING

import pytest

from enikk_contracts.deployments import AppResult, BuildFailed, DeploymentFinished
from enikk_core.deployments import DeploymentPolicy, DeploymentService
from enikk_core_domain.deployments import AppObservation, WatchTiming
from enikk_github.activity import CommitPull, WorkflowRun
from enikk_github.permissions import TokenPurpose
from enikk_kernel.ids import RandomIdGenerator

if TYPE_CHECKING:
    from collections.abc import Sequence

    from conftest import Harness

    from enikk_github.app import InstallationToken

pytestmark = pytest.mark.anyio

OLD = "a" * 40
NEW = "b" * 40
APPS = ("enikk-core", "enikk-runtime-claude")
POLICY = DeploymentPolicy(
    apps=APPS,
    repository="project-enikk",
    timing=WatchTiming(timeout=timedelta(minutes=10), settle=timedelta(seconds=60)),
)


class FakeReader:
    def __init__(self) -> None:
        self.observations: list[AppObservation] = []

    async def observe(self, apps: Sequence[str]) -> list[AppObservation]:
        return [o for o in self.observations if o.app in apps]

    def show(
        self, commit: str, started: datetime, phase: str = "Succeeded", health: str = "Healthy"
    ) -> None:
        self.observations = [
            AppObservation(
                app=app,
                image_tag=f"sha-{commit}",
                operation_started_at=started,
                operation_phase=phase,
                operation_revision="c" * 40,
                operation_message="PostSync hook failed" if phase == "Failed" else "",
                health=health,
            )
            for app in APPS
        ]


class FakeActivity:
    def __init__(self) -> None:
        self.runs: list[WorkflowRun] = []

    async def workflow_runs(
        self, token: InstallationToken, workflow: str, branch: str = "main"
    ) -> list[WorkflowRun]:
        assert workflow == "images.yml"
        return list(self.runs)

    async def pull_of_commit(self, token: InstallationToken, sha: str) -> CommitPull | None:
        return CommitPull(number=40, title="feat: something", url="https://github.com/x/y/pull/40")


@pytest.fixture
def reader() -> FakeReader:
    return FakeReader()


@pytest.fixture
def activity() -> FakeActivity:
    return FakeActivity()


@pytest.fixture
def service(harness: Harness, reader: FakeReader, activity: FakeActivity) -> DeploymentService:
    return DeploymentService(
        uow=harness.db.unit_of_work,
        reader=reader,
        app=harness.github,
        activity=activity,
        policy=POLICY,
        clock=harness.clock,
        ids=RandomIdGenerator(),
    )


async def _notices(harness: Harness) -> list[object]:
    async with harness.db.unit_of_work() as uow:
        return [n.body for n in await uow.notices.pending(50)]


async def test_successful_deployment_is_reported_once_with_its_pull_request(
    harness: Harness, service: DeploymentService, reader: FakeReader
) -> None:
    reader.show(OLD, harness.clock.now())
    await service.watch_once()
    assert await _notices(harness) == []

    harness.clock.advance(timedelta(seconds=30))
    reader.show(NEW, harness.clock.now())
    await service.watch_once()
    harness.clock.advance(timedelta(seconds=61))
    await service.watch_once()
    await service.watch_once()

    [notice] = await _notices(harness)
    assert isinstance(notice, DeploymentFinished)
    assert notice.succeeded
    assert notice.commit == NEW
    assert notice.repository == "404Vector/project-enikk"
    assert notice.pull is not None
    assert notice.pull.number == 40
    assert {a.app for a in notice.apps} == set(APPS)
    assert harness.github.issued[-1] == ("project-enikk", TokenPurpose.WATCH)
    assert len(harness.github.revoked) == len(harness.github.issued)


async def test_failed_deployment_says_why(
    harness: Harness, service: DeploymentService, reader: FakeReader
) -> None:
    reader.show(OLD, harness.clock.now())
    await service.watch_once()
    harness.clock.advance(timedelta(seconds=30))
    reader.show(NEW, harness.clock.now(), phase="Failed")
    await service.watch_once()
    harness.clock.advance(timedelta(seconds=61))
    await service.watch_once()

    [notice] = await _notices(harness)
    assert isinstance(notice, DeploymentFinished)
    assert not notice.succeeded
    assert {a.result for a in notice.apps} == {AppResult.FAILED}
    assert notice.apps[0].detail == "PostSync hook failed"


async def test_failed_image_builds_are_reported_once(
    harness: Harness, service: DeploymentService, reader: FakeReader, activity: FakeActivity
) -> None:
    reader.show(OLD, harness.clock.now())
    old_failure = WorkflowRun(
        id=1,
        head_sha=OLD,
        completed=True,
        conclusion="failure",
        url="https://github.com/x/y/actions/runs/1",
        created_at=harness.clock.now() - timedelta(hours=1),
    )
    activity.runs = [old_failure]
    await service.watch_once()
    assert await _notices(harness) == []

    harness.clock.advance(timedelta(minutes=5))
    activity.runs = [
        old_failure,
        old_failure.model_copy(
            update={"id": 2, "head_sha": NEW, "created_at": harness.clock.now()}
        ),
        old_failure.model_copy(
            update={"id": 3, "conclusion": "cancelled", "created_at": harness.clock.now()}
        ),
    ]
    await service.watch_once()
    await service.watch_once()
    [notice] = await _notices(harness)
    assert isinstance(notice, BuildFailed)
    assert notice.commit == NEW
    assert notice.conclusion == "failure"
