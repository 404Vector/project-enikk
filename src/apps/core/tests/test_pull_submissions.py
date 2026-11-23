"""A Slack thread's commits pushed by core and opened as a PR (ADR-0037 §6b)."""

from __future__ import annotations

import base64
import uuid
from typing import TYPE_CHECKING

import pytest
from pydantic import SecretStr

from enikk_contracts.agents import PlatformTool, ToolPolicy
from enikk_contracts.common import ConnectorKind, ConversationRef
from enikk_contracts.events import InboundEvent
from enikk_contracts.pulls import PullSubmission
from enikk_contracts.tasks import LeaseRequest
from enikk_core.submissions import PullSubmissions, SubmissionPolicy
from enikk_core_client import CoreApiError
from enikk_core_domain.errors import InvalidRequestError
from enikk_core_domain.testing import agent, conversation
from enikk_github.clone import Author, Pushed
from enikk_github.permissions import TokenPurpose
from enikk_github.pulls import PullInfo

if TYPE_CHECKING:
    from conftest import Harness

    from enikk_contracts.tasks import TaskAssignment
    from enikk_core_client import CoreClient
    from enikk_github.app import InstallationToken

pytestmark = pytest.mark.anyio

OWNER = "U0OWNER"
BASE = "a" * 40
HEAD = "b" * 40
PUSHED = "c" * 40
BUNDLE = base64.b64encode(b"# v2 git bundle\n").decode()


class FakeClone:
    def __init__(self, error: Exception | None = None) -> None:
        self.pushes: list[dict[str, object]] = []
        self._error = error

    def push(self, bundle: bytes, **kwargs: object) -> Pushed:
        self.pushes.append({"bundle": bundle, **kwargs})
        if self._error is not None:
            raise self._error
        return Pushed(head=PUSHED, commits=2, files=("docs/a.md",))

    def subject(self, commit: str) -> str:
        return "docs: add a"


class FakePulls:
    def __init__(self) -> None:
        self.open: PullInfo | None = None
        self.created: list[dict[str, object]] = []
        self.edited: list[tuple[int, str | None, str | None]] = []

    def _pull(self, branch: str, title: str) -> PullInfo:
        return PullInfo(
            number=81,
            title=title,
            url="https://github.com/404Vector/project-enikk/pull/81",
            body="",
            draft=False,
            open=True,
            merged=False,
            author="enikk-deployer[bot]",
            head_sha=PUSHED,
            branch=branch,
            from_fork=False,
        )

    async def open_for_branch(self, token: InstallationToken, branch: str) -> PullInfo | None:
        return self.open

    async def create(
        self, token: InstallationToken, *, branch: str, title: str, body: str, base: str = "main"
    ) -> PullInfo:
        self.created.append({"branch": branch, "title": title, "body": body, "base": base})
        self.open = self._pull(branch, title)
        return self.open

    async def edit(
        self, token: InstallationToken, number: int, *, title: str | None, body: str | None
    ) -> PullInfo:
        self.edited.append((number, title, body))
        assert self.open is not None
        return self.open


class World:
    def __init__(self, harness: Harness, clone: FakeClone | None = None) -> None:
        self.clone = clone or FakeClone()
        self.pulls = FakePulls()
        harness.services.submissions = PullSubmissions(
            uow=harness.db.unit_of_work,
            catalog=harness.catalog,
            app=harness.github,
            pulls=self.pulls,
            clone=self.clone,
            policy=SubmissionPolicy(repository="project-enikk"),
        )


def _grant(harness: Harness, *, grant: bool = True) -> None:
    tools = ToolPolicy(platform=(PlatformTool.PULL_REQUESTS,) if grant else ())
    harness.catalog.put(agent("assistant").model_copy(update={"tools": tools}))


async def _leased(harness: Harness, client: CoreClient, thread: str = "9.0") -> TaskAssignment:
    await harness.usecases.accept_event(
        InboundEvent(
            event_id=f"Ev-{uuid.uuid4()}",
            conversation=ConversationRef(
                connector=ConnectorKind.SLACK, channel="C1", thread=thread
            ),
            user=OWNER,
            text="README 를 고쳐서 PR 로 올려 줘",
            agent="assistant",
        )
    )
    response = await client.lease_task(LeaseRequest(wait_seconds=0))
    assert response.assignment is not None
    return response.assignment


def _submission(lease: TaskAssignment, **changes: object) -> PullSubmission:
    fields: dict[str, object] = {
        "lease_id": lease.lease_id,
        "bundle": BUNDLE,
        "base": BASE,
        "head": HEAD,
        **changes,
    }
    return PullSubmission.model_validate(fields)


async def test_the_branch_comes_from_the_thread_and_a_pr_is_opened(
    harness: Harness, claude: CoreClient
) -> None:
    _grant(harness)
    world = World(harness)
    lease = await _leased(harness, claude)
    submitted = await claude.submit_pull(lease.task_id, _submission(lease, head=HEAD))

    branch = f"agent/{lease.session_id}"
    assert (submitted.branch, submitted.number, submitted.created) == (branch, 81, True)
    assert submitted.head == PUSHED
    [push] = world.clone.pushes
    assert push["branch"] == branch
    assert push["session_ref"] == f"refs/heads/session/{lease.session_id}"
    assert (push["base"], push["head"], push["bundle"]) == (BASE, HEAD, b"# v2 git bundle\n")
    assert push["author"] == Author(
        name="enikk-deployer[bot]", email="42+enikk-deployer[bot]@users.noreply.github.com"
    )
    token = push["token"]
    assert isinstance(token, SecretStr)
    [created] = world.pulls.created
    assert created["title"] == "docs: add a"
    assert created["base"] == "main"
    assert harness.github.issued == [("project-enikk", TokenPurpose.SUBMIT)]
    assert harness.github.revoked == ["project-enikk"]


async def test_a_second_submission_from_the_thread_updates_its_pr(
    harness: Harness, claude: CoreClient
) -> None:
    _grant(harness)
    world = World(harness)
    lease = await _leased(harness, claude)
    await claude.submit_pull(lease.task_id, _submission(lease))
    again = await claude.submit_pull(lease.task_id, _submission(lease, title="docs: a and b"))

    assert again.created is False
    assert len(world.pulls.created) == 1
    assert world.pulls.edited == [(81, "docs: a and b", None)]
    assert {p["branch"] for p in world.clone.pushes} == {f"agent/{lease.session_id}"}


async def test_roles_without_the_grant_cannot_submit(harness: Harness, claude: CoreClient) -> None:
    _grant(harness, grant=False)
    world = World(harness)
    lease = await _leased(harness, claude)
    with pytest.raises(CoreApiError, match="may not submit pull requests"):
        await claude.submit_pull(lease.task_id, _submission(lease))
    assert world.clone.pushes == []
    assert harness.github.issued == []


async def test_a_delegated_session_cannot_submit(harness: Harness, claude: CoreClient) -> None:
    _grant(harness)
    world = World(harness)
    lease = await _leased(harness, claude)
    session = harness.db.sessions[lease.session_id]
    harness.db.sessions[lease.session_id] = session.model_copy(
        update={"reply_to": conversation("8.0")}
    )
    with pytest.raises(CoreApiError, match="Slack thread"):
        await claude.submit_pull(lease.task_id, _submission(lease))
    assert world.clone.pushes == []


async def test_only_the_lease_holder_on_the_runtime_may_submit(
    harness: Harness, claude: CoreClient
) -> None:
    _grant(harness)
    world = World(harness)
    lease = await _leased(harness, claude)
    stale = _submission(lease).model_copy(update={"lease_id": uuid.uuid4()})
    with pytest.raises(CoreApiError):
        await claude.submit_pull(lease.task_id, stale)
    async with harness.client("slack") as slack:
        with pytest.raises(CoreApiError, match="may not call this endpoint"):
            await slack.submit_pull(lease.task_id, _submission(lease))
    assert world.clone.pushes == []


async def test_a_refused_bundle_reports_why_and_revokes_the_token(
    harness: Harness, claude: CoreClient
) -> None:
    _grant(harness)
    world = World(harness, FakeClone(InvalidRequestError("commit x is a merge commit")))
    lease = await _leased(harness, claude)
    with pytest.raises(CoreApiError, match="merge commit"):
        await claude.submit_pull(lease.task_id, _submission(lease))
    assert world.pulls.created == []
    assert harness.github.revoked == ["project-enikk"]


async def test_a_bundle_that_is_not_base64_is_refused(harness: Harness, claude: CoreClient) -> None:
    _grant(harness)
    world = World(harness)
    lease = await _leased(harness, claude)
    with pytest.raises(CoreApiError, match="base64"):
        await claude.submit_pull(lease.task_id, _submission(lease, bundle="abcde"))
    assert world.clone.pushes == []
