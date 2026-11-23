"""Deployment and build notices for the owner's DM (ADR-0023)."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import TYPE_CHECKING

import pytest

from enikk_contracts.deployments import (
    AppResult,
    BuildFailed,
    DeploymentFinished,
    RevertOutcome,
    RevertRequest,
)
from enikk_core.deployments import DeploymentPolicy, DeploymentService
from enikk_core_domain.deployments import AppObservation, WatchTiming
from enikk_github.activity import CommitPull, WorkflowRun
from enikk_github.permissions import TokenPurpose
from enikk_github.reverts import RevertPull, RevertRefused
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
        return CommitPull(
            number=40,
            title="feat: something",
            url="https://github.com/x/y/pull/40",
            branch="feat/x",
        )


class FakeReverter:
    def __init__(self) -> None:
        self.result: RevertPull | RevertRefused = RevertPull(
            number=41, url="https://github.com/x/y/pull/41", branch="agent/revert-b"
        )
        self.requested: list[str] = []

    async def open_revert(
        self, token: InstallationToken, sha: str, *, base: str = "main", note: str = ""
    ) -> RevertPull | RevertRefused:
        self.requested.append(sha)
        return self.result


@pytest.fixture
def reverter() -> FakeReverter:
    return FakeReverter()


@pytest.fixture
def reader() -> FakeReader:
    return FakeReader()


@pytest.fixture
def activity() -> FakeActivity:
    return FakeActivity()


@pytest.fixture
def service(
    harness: Harness, reader: FakeReader, activity: FakeActivity, reverter: FakeReverter
) -> DeploymentService:
    return DeploymentService(
        uow=harness.db.unit_of_work,
        reader=reader,
        app=harness.github,
        activity=activity,
        reverter=reverter,
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


async def test_revert_opens_a_pull_request_and_its_deployment_is_a_rollback(
    harness: Harness,
    service: DeploymentService,
    reader: FakeReader,
    activity: FakeActivity,
    reverter: FakeReverter,
) -> None:
    reader.show(OLD, harness.clock.now())
    await service.watch_once()
    result = await service.request_revert(RevertRequest(commit=NEW, requested_by="U1"))
    assert result.outcome is RevertOutcome.OPENED
    assert result.pull is not None
    assert result.pull.number == 41
    assert reverter.requested == [NEW]
    assert ("project-enikk", TokenPurpose.DEVELOP) in harness.github.issued

    async def merged_revert(token: InstallationToken, sha: str) -> CommitPull:
        return CommitPull(
            number=41,
            title='Revert "x"',
            url="https://github.com/x/y/pull/41",
            branch="agent/revert-x",
        )

    activity.pull_of_commit = merged_revert  # type: ignore[method-assign]
    harness.clock.advance(timedelta(seconds=30))
    reverted = "9" * 40
    reader.show(reverted, harness.clock.now())
    await service.watch_once()
    harness.clock.advance(timedelta(seconds=61))
    await service.watch_once()
    [notice] = await _notices(harness)
    assert isinstance(notice, DeploymentFinished)
    assert notice.succeeded
    assert notice.rollback_of == NEW


async def test_refused_revert_says_why(service: DeploymentService, reverter: FakeReverter) -> None:
    reverter.result = RevertRefused(
        reason="later commits changed the same files", conflicts=("README.md",)
    )
    result = await service.request_revert(RevertRequest(commit=NEW, requested_by="U1"))
    assert result.outcome is RevertOutcome.REFUSED
    assert "README.md" in result.detail


async def _delegated_branch(harness: Harness) -> str:
    import uuid

    from enikk_core_domain.model import Session
    from enikk_core_domain.testing import T0, agent, conversation, task

    session = Session.start(
        session_id=uuid.uuid4(),
        conversation=conversation("9.0:developer"),
        agent=agent("developer"),
        now=T0,
        reply_to=conversation("9.0"),
    )
    async with harness.db.unit_of_work() as uow:
        await uow.sessions.add(session)
        await uow.tasks.add(task(session))
        await uow.commit()
    return f"agent/{session.id}"


def _carded(
    harness: Harness, reader: FakeReader, activity: FakeActivity, reverter: FakeReverter
) -> DeploymentService:
    from enikk_core.threads import ThreadNotes

    return DeploymentService(
        uow=harness.db.unit_of_work,
        reader=reader,
        app=harness.github,
        activity=activity,
        reverter=reverter,
        policy=POLICY,
        clock=harness.clock,
        ids=RandomIdGenerator(),
        threads=ThreadNotes(harness.clock, RandomIdGenerator()),
    )


async def test_deployment_results_update_the_status_card_of_an_agent_pr(
    harness: Harness, reader: FakeReader, activity: FakeActivity, reverter: FakeReverter
) -> None:
    from enikk_contracts.approvals import PullStage, PullStatusCard

    branch = await _delegated_branch(harness)
    reverted = "9" * 40

    async def pull_of(token: InstallationToken, sha: str) -> CommitPull:
        if sha == reverted:
            return CommitPull(
                number=41, title="Revert", url="https://github.com/x/y/pull/41", branch="agent/r"
            )
        return CommitPull(
            number=40, title="feat: x", url="https://github.com/x/y/pull/40", branch=branch
        )

    activity.pull_of_commit = pull_of  # type: ignore[method-assign]
    service = _carded(harness, reader, activity, reverter)
    reader.show(OLD, harness.clock.now())
    await service.watch_once()
    harness.clock.advance(timedelta(seconds=30))
    reader.show(NEW, harness.clock.now(), phase="Failed")
    await service.watch_once()
    harness.clock.advance(timedelta(seconds=61))
    await service.watch_once()

    cards = [n for n in await _notices(harness) if isinstance(n, PullStatusCard)]
    [failed] = cards
    assert failed.stage is PullStage.DEPLOY_FAILED
    assert failed.number == 40
    assert failed.commit == NEW
    assert "enikk-core" in failed.detail

    await service.request_revert(RevertRequest(commit=NEW, requested_by="U1"))
    harness.clock.advance(timedelta(seconds=30))
    reader.show(reverted, harness.clock.now())
    await service.watch_once()
    harness.clock.advance(timedelta(seconds=61))
    await service.watch_once()
    cards = [n for n in await _notices(harness) if isinstance(n, PullStatusCard)]
    assert [(c.number, c.stage) for c in cards[1:]] == [(40, PullStage.ROLLED_BACK)]


async def test_a_failed_build_updates_the_status_card(
    harness: Harness, reader: FakeReader, activity: FakeActivity, reverter: FakeReverter
) -> None:
    from enikk_contracts.approvals import PullStage, PullStatusCard

    branch = await _delegated_branch(harness)

    async def pull_of(token: InstallationToken, sha: str) -> CommitPull:
        return CommitPull(
            number=40, title="feat: x", url="https://github.com/x/y/pull/40", branch=branch
        )

    activity.pull_of_commit = pull_of  # type: ignore[method-assign]
    service = _carded(harness, reader, activity, reverter)
    reader.show(OLD, harness.clock.now())
    await service.watch_once()
    activity.runs = [
        WorkflowRun(
            id=2,
            head_sha=NEW,
            completed=True,
            conclusion="failure",
            url="https://github.com/x/y/actions/runs/2",
            created_at=harness.clock.now() + timedelta(minutes=1),
        )
    ]
    harness.clock.advance(timedelta(minutes=5))
    await service.watch_once()
    [card] = [n for n in await _notices(harness) if isinstance(n, PullStatusCard)]
    assert card.stage is PullStage.BUILD_FAILED
    assert card.commit == NEW
