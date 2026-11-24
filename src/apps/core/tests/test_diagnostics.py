"""Read-only diagnostics (ADR-0029)."""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import TYPE_CHECKING

import pytest

from enikk_contracts.deployments import AppDeployment, AppResult, DeploymentFinished
from enikk_contracts.diagnostics import (
    DeploymentsQuery,
    LogsQuery,
    RecentTasksQuery,
    Service,
    TaskDetailQuery,
)
from enikk_contracts.tasks import TaskStatus
from enikk_core.diagnostics import SELECTORS, DiagnosticsPolicy, DiagnosticsService
from enikk_core_domain.model import Notice
from enikk_core_domain.redaction import MASK, looks_secret, redact
from enikk_core_domain.testing import session, task

if TYPE_CHECKING:
    from conftest import Harness

pytestmark = pytest.mark.anyio

LEASE = uuid.uuid4()
SECRET = "xoxb-" + "1234567890-abcdefghij"


class FakeLogs:
    def __init__(self) -> None:
        self.asked: list[tuple[str, str]] = []

    async def pods(self, namespace: str, selector: str) -> list[str]:
        self.asked.append((namespace, selector))
        return ["pod-a"]

    async def tail(self, namespace: str, pod: str, since_seconds: int) -> list[str]:
        return ["started", f"token {SECRET} rejected", "vault sync done"]


@pytest.fixture
def logs() -> FakeLogs:
    return FakeLogs()


@pytest.fixture
def diag(harness: Harness, logs: FakeLogs) -> DiagnosticsService:
    return DiagnosticsService(
        uow=harness.db.unit_of_work,
        clock=harness.clock,
        policy=DiagnosticsPolicy(),
        logs=logs,
    )


def test_secrets_are_masked() -> None:
    assert looks_secret(SECRET)
    assert redact(f"a {SECRET} b") == f"a {MASK} b"
    assert not looks_secret("plain text with sk words")


async def test_recent_tasks_and_detail_show_failures(
    harness: Harness, diag: DiagnosticsService
) -> None:
    owner = session()
    first = task(owner, now=harness.clock.now() - timedelta(minutes=5))
    second = task(owner, now=harness.clock.now())
    failed = second.model_copy(
        update={"status": TaskStatus.FAILED, "failure": f"git failed with {SECRET}"}
    )
    async with harness.db.unit_of_work() as uow:
        await uow.sessions.add(owner)
        await uow.tasks.add(first)
        await uow.tasks.add(failed)
        await uow.commit()
    recent = await diag.recent_tasks(RecentTasksQuery(lease_id=LEASE, status=TaskStatus.FAILED))
    [summary] = recent.tasks
    assert summary.task_id == second.id
    assert summary.failure is not None
    assert MASK in summary.failure
    assert SECRET not in summary.failure
    detail = await diag.task_detail(TaskDetailQuery(lease_id=LEASE, task_id=second.id))
    assert detail.previous is not None
    assert detail.previous.task_id == first.id


async def test_logs_are_masked_and_filtered(diag: DiagnosticsService, logs: FakeLogs) -> None:
    found = await diag.logs(LogsQuery(lease_id=LEASE, service=Service.CORE, contains="token"))
    assert found.lines == (f"pod-a: token {MASK} rejected",)
    assert logs.asked == [("enikk", "app.kubernetes.io/name=enikk-core")]


async def test_deployments_come_from_the_notices(
    harness: Harness, diag: DiagnosticsService
) -> None:
    body = DeploymentFinished(
        repository="404Vector/project-enikk",
        commit="a" * 40,
        succeeded=False,
        apps=(AppDeployment(app="enikk-core", result=AppResult.FAILED, detail="Degraded"),),
    )
    async with harness.db.unit_of_work() as uow:
        await uow.notices.add(Notice(id=uuid.uuid4(), body=body, created_at=harness.clock.now()))
        await uow.commit()
    found = await diag.deployments(DeploymentsQuery(lease_id=LEASE))
    [event] = found.events
    assert (event.kind, event.succeeded) == ("deployment", False)
    assert "enikk-core: Degraded" in event.summary


def test_every_service_has_a_log_selector() -> None:
    assert set(SELECTORS) == set(Service)
    assert SELECTORS[Service.SCHEDULER] == "app.kubernetes.io/name=enikk-scheduler"
    assert SELECTORS[Service.AGENTGATEWAY] == "app.kubernetes.io/name=enikk-agentgateway"
