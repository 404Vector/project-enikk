"""Prompt candidates and the owner's promotion in Slack (ADR-0032, I-36)."""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

import httpx
import pydantic_core
import pytest

from enikk_contracts.agents import PlatformTool, ToolPolicy
from enikk_contracts.common import ConnectorKind, ConversationRef
from enikk_contracts.events import InboundEvent
from enikk_contracts.prompts import (
    PromotionAction,
    PromotionDecision,
    PromotionPosted,
    PromotionRequested,
    PromotionStatus,
    PromptCandidateRequest,
    PromptConfigChange,
    PromptReadRequest,
)
from enikk_contracts.tasks import LeaseRequest
from enikk_core.prompts import PromptService, load_owner
from enikk_core_client import CoreApiError
from enikk_core_domain.errors import (
    InvalidRequestError,
    NotFoundError,
    PermissionDeniedError,
)
from enikk_core_domain.prompts import CANDIDATE, PRODUCTION, StoredPrompt
from enikk_core_domain.testing import agent
from enikk_kernel.errors import ConfigurationError
from enikk_kernel.ids import RandomIdGenerator

if TYPE_CHECKING:
    from pathlib import Path

    from conftest import Harness
    from pydantic import JsonValue

    from enikk_contracts.tasks import TaskAssignment
    from enikk_core_client import CoreClient

pytestmark = pytest.mark.anyio

OWNER = "U0OWNER"
TEXT = "You are enikk. Answer briefly."
NOW = LeaseRequest(wait_seconds=0)


class FakeLangfuse:
    """Prompt versions and labels, as Langfuse keeps them: a label is on one version."""

    def __init__(self) -> None:
        self.versions: dict[str, list[StoredPrompt]] = {}
        self.created: list[tuple[str, str]] = []
        self.production_moves: list[tuple[str, int]] = []
        self.fail_writes = False

    def put(self, name: str, text: str, config: dict[str, JsonValue], *labels: str) -> int:
        for label in labels:
            self._drop(name, label)
        versions = self.versions.setdefault(name, [])
        versions.append(
            StoredPrompt(
                name=name,
                version=len(versions) + 1,
                type="text",
                text=text,
                config=config,
                labels=labels,
            )
        )
        return len(versions)

    def production(self, name: str) -> int | None:
        found = next((p for p in self.versions.get(name, []) if PRODUCTION in p.labels), None)
        return None if found is None else found.version

    def _drop(self, name: str, label: str) -> None:
        self.versions[name] = [
            p.model_copy(update={"labels": tuple(x for x in p.labels if x != label)})
            for p in self.versions.get(name, [])
        ]

    async def get(
        self, name: str, *, label: str | None = None, version: int | None = None
    ) -> StoredPrompt | None:
        for p in self.versions.get(name, []):
            if (label is not None and label in p.labels) or p.version == version:
                return p
        return None

    async def create_candidate(
        self, name: str, text: str, config: dict[str, JsonValue], message: str
    ) -> StoredPrompt:
        if self.fail_writes:
            raise AssertionError("no write expected")
        self.created.append((name, message))
        version = self.put(name, text, config, CANDIDATE)
        return self.versions[name][version - 1]

    async def set_production(self, name: str, version: int) -> None:
        if self.fail_writes:
            raise AssertionError("no write expected")
        self.production_moves.append((name, version))
        self._drop(name, PRODUCTION)
        self.versions[name] = [
            p.model_copy(update={"labels": (*p.labels, PRODUCTION)}) if p.version == version else p
            for p in self.versions[name]
        ]


@pytest.fixture
def langfuse() -> FakeLangfuse:
    fake = FakeLangfuse()
    fake.put("assistant", "v1 text", {"model": "claude-old"})
    fake.put("assistant", "v2 text", {"model": "claude-a"})
    fake.put("assistant", "You are enikk.", {"model": "claude-a", "effort": "low"}, PRODUCTION)
    fake.put("developer", "dev text", {"model": "claude-d"}, PRODUCTION)
    return fake


@pytest.fixture
def prompts(harness: Harness, langfuse: FakeLangfuse) -> PromptService:
    granted = agent("assistant").model_copy(
        update={"tools": ToolPolicy(platform=(PlatformTool.PROMPT_CANDIDATES,))}
    )
    harness.catalog.put(granted)
    service = PromptService(
        uow=harness.db.unit_of_work,
        catalog=harness.catalog,
        registry=langfuse,
        owner=OWNER,
        clock=harness.clock,
        ids=RandomIdGenerator(),
    )
    harness.services.prompts = service
    return service


async def _leased(
    harness: Harness, client: CoreClient, agent_name: str = "assistant"
) -> TaskAssignment:
    await harness.usecases.accept_event(
        InboundEvent(
            event_id=f"Ev-{uuid.uuid4()}",
            conversation=ConversationRef(connector=ConnectorKind.SLACK, channel="C1", thread="9.0"),
            user=OWNER,
            text="assistant 프롬프트를 더 짧게 답하도록 바꿔 줘",
            agent=agent_name,
        )
    )
    response = await client.lease_task(NOW)
    assert response.assignment is not None
    return response.assignment


def _request(lease: uuid.UUID, **changes: object) -> PromptCandidateRequest:
    fields: dict[str, object] = {
        "lease_id": lease,
        "prompt": "assistant",
        "text": TEXT,
        "reason": "답을 더 짧게",
    }
    fields.update(changes)
    return PromptCandidateRequest.model_validate(fields)


async def _outbox(harness: Harness) -> list[str]:
    return [m.text for m in harness.db.outbox.values()]


# ---------------------------------------------------------------- candidates


async def test_candidate_is_created_without_touching_production(
    harness: Harness, prompts: PromptService, langfuse: FakeLangfuse, claude: CoreClient
) -> None:
    lease = await _leased(harness, claude)
    created = await claude.propose_prompt(lease.task_id, _request(lease.lease_id))
    assert (created.production_version, created.candidate_version) == (3, 4)
    assert langfuse.production("assistant") == 3
    assert langfuse.production_moves == []
    candidate = langfuse.versions["assistant"][3]
    assert candidate.labels == (CANDIDATE,)
    assert candidate.text == TEXT
    assert candidate.config == {"model": "claude-a", "effort": "low"}
    [(_, message)] = langfuse.created
    assert message == "답을 더 짧게"

    [notice] = harness.db.notices.values()
    body = notice.body
    assert isinstance(body, PromotionRequested)
    assert body.promotion_id == created.promotion_id
    assert body.roles == ("assistant",)
    assert (body.production_version, body.candidate_version) == (3, 4)
    assert "-You are enikk." in body.text_diff
    assert f"+{TEXT}" in body.text_diff
    assert body.config_diff == ""
    assert body.origin == ConversationRef(connector=ConnectorKind.SLACK, channel="C1", thread="9.0")
    [promotion] = harness.db.prompt_promotions.values()
    assert promotion.status is PromotionStatus.REQUESTED


async def test_config_change_keeps_the_rest_and_shows_in_the_diff(
    harness: Harness, prompts: PromptService, langfuse: FakeLangfuse, claude: CoreClient
) -> None:
    lease = await _leased(harness, claude)
    request = _request(
        lease.lease_id, text="You are enikk.", config=PromptConfigChange(model="claude-b")
    )
    await claude.propose_prompt(lease.task_id, request)
    assert langfuse.versions["assistant"][3].config == {"model": "claude-b", "effort": "low"}
    [notice] = harness.db.notices.values()
    assert isinstance(notice.body, PromotionRequested)
    assert '-  "model": "claude-a"' in notice.body.config_diff
    assert '+  "model": "claude-b"' in notice.body.config_diff
    assert notice.body.text_diff == ""


@pytest.mark.parametrize(
    ("setup", "changes", "message"),
    [
        (None, {"prompt": "painter-v2"}, "not the prompt of any role"),
        (None, {"text": "You are enikk."}, "same as production"),
        (None, {"text": "token ghp_" + "a" * 36}, "secret"),
        ("no-model", {}, 'no "model"'),
        ("chat", {}, "roles need text prompts"),
    ],
)
async def test_invalid_candidates_are_not_created(
    harness: Harness,
    prompts: PromptService,
    langfuse: FakeLangfuse,
    claude: CoreClient,
    setup: str | None,
    changes: dict[str, object],
    message: str,
) -> None:
    if setup == "no-model":
        langfuse.put("assistant", "You are enikk.", {"effort": "low"}, PRODUCTION)
    if setup == "chat":
        langfuse.put("assistant", "x", {"model": "claude-a"}, PRODUCTION)
        langfuse.versions["assistant"][-1] = langfuse.versions["assistant"][-1].model_copy(
            update={"type": "chat", "text": None}
        )
    lease = await _leased(harness, claude)
    with pytest.raises(CoreApiError, match=message):
        await claude.propose_prompt(lease.task_id, _request(lease.lease_id, **changes))
    assert langfuse.created == []
    assert harness.db.prompt_promotions == {}
    assert harness.db.notices == {}


async def test_only_roles_granted_prompt_candidates_may_propose(
    harness: Harness, prompts: PromptService, langfuse: FakeLangfuse, claude: CoreClient
) -> None:
    harness.catalog.put(agent("assistant"))
    lease = await _leased(harness, claude)
    with pytest.raises(CoreApiError, match="may not create prompt candidates"):
        await claude.propose_prompt(lease.task_id, _request(lease.lease_id))
    assert langfuse.created == []


async def test_the_candidate_request_cannot_carry_labels(
    harness: Harness, prompts: PromptService, langfuse: FakeLangfuse, claude: CoreClient
) -> None:
    lease = await _leased(harness, claude)
    body = _request(lease.lease_id).model_dump(mode="json")
    body["labels"] = [PRODUCTION]
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=harness.app), base_url="http://core.test"
    ) as http:
        response = await http.post(
            f"/v1/tasks/{lease.task_id}/prompts/candidates",
            content=pydantic_core.to_json(body),
            headers={"Authorization": "Bearer claude", "Content-Type": "application/json"},
        )
    assert response.status_code == 422
    assert langfuse.created == []
    assert langfuse.production("assistant") == 3


async def test_runtimes_cannot_decide_and_connectors_cannot_propose(
    harness: Harness, prompts: PromptService, langfuse: FakeLangfuse, claude: CoreClient
) -> None:
    lease = await _leased(harness, claude)
    created = await claude.propose_prompt(lease.task_id, _request(lease.lease_id))
    decision = PromotionDecision(action=PromotionAction.PROMOTE, decided_by=OWNER)
    with pytest.raises(CoreApiError, match="may not call this endpoint"):
        await claude.decide_promotion(created.promotion_id, decision)
    async with harness.client("slack") as slack:
        with pytest.raises(CoreApiError, match="may not call this endpoint"):
            await slack.propose_prompt(lease.task_id, _request(lease.lease_id))
    async with harness.client("dev") as dev:
        with pytest.raises(CoreApiError):
            await dev.propose_prompt(lease.task_id, _request(lease.lease_id))
    assert langfuse.production("assistant") == 3


async def test_a_stale_lease_cannot_propose(
    harness: Harness, prompts: PromptService, langfuse: FakeLangfuse, claude: CoreClient
) -> None:
    lease = await _leased(harness, claude)
    with pytest.raises(CoreApiError):
        await claude.propose_prompt(lease.task_id, _request(uuid.uuid4()))
    assert langfuse.created == []


# ---------------------------------------------------------------- decisions


async def _proposed(harness: Harness, claude: CoreClient) -> uuid.UUID:
    lease = await _leased(harness, claude)
    return (await claude.propose_prompt(lease.task_id, _request(lease.lease_id))).promotion_id


def _press(action: PromotionAction, by: str = OWNER) -> PromotionDecision:
    return PromotionDecision(action=action, decided_by=by)


async def test_promotion_moves_production_and_tells_the_thread(
    harness: Harness, prompts: PromptService, langfuse: FakeLangfuse, claude: CoreClient
) -> None:
    promotion_id = await _proposed(harness, claude)
    async with harness.client("slack") as slack:
        result = await slack.decide_promotion(promotion_id, _press(PromotionAction.PROMOTE))
    assert result.status is PromotionStatus.PROMOTED
    assert result.changed
    assert "assistant 역할은 최대 60초 안에" in result.detail
    assert langfuse.production("assistant") == 4
    assert langfuse.production_moves == [("assistant", 4)]
    [note] = await _outbox(harness)
    assert "production 으로 승격했습니다 (v3 → v4)" in note
    promotion = harness.db.prompt_promotions[promotion_id]
    assert (promotion.status, promotion.decided_by) == (PromotionStatus.PROMOTED, OWNER)


async def test_a_repeated_press_changes_nothing(
    harness: Harness, prompts: PromptService, langfuse: FakeLangfuse, claude: CoreClient
) -> None:
    promotion_id = await _proposed(harness, claude)
    first = await prompts.decide(promotion_id, _press(PromotionAction.PROMOTE))
    again = await prompts.decide(promotion_id, _press(PromotionAction.PROMOTE))
    rejected_late = await prompts.decide(promotion_id, _press(PromotionAction.REJECT))
    assert first.changed
    assert not again.changed
    assert again.status is PromotionStatus.PROMOTED
    assert not rejected_late.changed
    assert langfuse.production_moves == [("assistant", 4)]
    assert len(await _outbox(harness)) == 1


async def test_someone_other_than_the_owner_is_refused(
    harness: Harness, prompts: PromptService, langfuse: FakeLangfuse, claude: CoreClient
) -> None:
    promotion_id = await _proposed(harness, claude)
    async with harness.client("slack") as slack:
        with pytest.raises(CoreApiError, match="only the owner"):
            await slack.decide_promotion(
                promotion_id, _press(PromotionAction.PROMOTE, by="U0SOMEONE")
            )
    assert langfuse.production("assistant") == 3
    assert harness.db.prompt_promotions[promotion_id].status is PromotionStatus.REQUESTED


async def test_promotion_is_refused_when_production_changed_meanwhile(
    harness: Harness, prompts: PromptService, langfuse: FakeLangfuse, claude: CoreClient
) -> None:
    promotion_id = await _proposed(harness, claude)
    await langfuse.set_production("assistant", 2)
    langfuse.production_moves.clear()
    result = await prompts.decide(promotion_id, _press(PromotionAction.PROMOTE))
    assert result.status is PromotionStatus.STALE
    assert "v3 에서 v2 로 바뀌어" in result.detail
    assert langfuse.production_moves == []
    assert langfuse.production("assistant") == 2
    [note] = await _outbox(harness)
    assert "다시 요청해 주세요" in note


async def test_rejection_keeps_langfuse_and_the_candidate(
    harness: Harness, prompts: PromptService, langfuse: FakeLangfuse, claude: CoreClient
) -> None:
    promotion_id = await _proposed(harness, claude)
    langfuse.fail_writes = True
    result = await prompts.decide(promotion_id, _press(PromotionAction.REJECT))
    assert (result.status, result.changed) == (PromotionStatus.REJECTED, True)
    assert langfuse.production("assistant") == 3
    assert langfuse.versions["assistant"][3].labels == (CANDIDATE,)
    [note] = await _outbox(harness)
    assert "거절" in note
    later = await prompts.decide(promotion_id, _press(PromotionAction.PROMOTE))
    assert (later.status, later.changed) == (PromotionStatus.REJECTED, False)


async def test_revert_moves_production_back_once(
    harness: Harness, prompts: PromptService, langfuse: FakeLangfuse, claude: CoreClient
) -> None:
    promotion_id = await _proposed(harness, claude)
    early = await prompts.decide(promotion_id, _press(PromotionAction.REVERT))
    assert not early.changed
    await prompts.decide(promotion_id, _press(PromotionAction.PROMOTE))
    reverted = await prompts.decide(promotion_id, _press(PromotionAction.REVERT))
    again = await prompts.decide(promotion_id, _press(PromotionAction.REVERT))
    assert (reverted.status, reverted.changed) == (PromotionStatus.REVERTED, True)
    assert not again.changed
    assert langfuse.production("assistant") == 3
    assert langfuse.production_moves == [("assistant", 4), ("assistant", 3)]
    assert "v3 로 되돌렸습니다" in (await _outbox(harness))[-1]


async def test_revert_is_refused_when_another_version_was_promoted(
    harness: Harness, prompts: PromptService, langfuse: FakeLangfuse, claude: CoreClient
) -> None:
    promotion_id = await _proposed(harness, claude)
    await prompts.decide(promotion_id, _press(PromotionAction.PROMOTE))
    await langfuse.set_production("assistant", 2)
    result = await prompts.decide(promotion_id, _press(PromotionAction.REVERT))
    assert (result.status, result.changed) == (PromotionStatus.PROMOTED, False)
    assert "지금 production 은 v2" in result.detail
    assert langfuse.production("assistant") == 2


async def test_unknown_promotion(prompts: PromptService) -> None:
    with pytest.raises(NotFoundError):
        await prompts.decide(uuid.uuid4(), _press(PromotionAction.PROMOTE))


async def test_posting_the_dm_tells_the_asking_thread_its_link(
    harness: Harness, prompts: PromptService, claude: CoreClient
) -> None:
    promotion_id = await _proposed(harness, claude)
    async with harness.client("slack") as slack:
        linked = await slack.promotion_posted(
            promotion_id, PromotionPosted(link="https://slack.test/archives/D1/p1")
        )
    assert linked.told_origin
    [note] = await _outbox(harness)
    assert "승격 요청을 소유자 DM 으로 보냈습니다: https://slack.test/archives/D1/p1" in note


async def test_developer_prompt_takes_effect_from_the_next_task(
    harness: Harness, prompts: PromptService, langfuse: FakeLangfuse, claude: CoreClient
) -> None:
    lease = await _leased(harness, claude)
    request = _request(lease.lease_id, prompt="developer", text="You change code carefully.")
    created = await claude.propose_prompt(lease.task_id, request)
    result = await prompts.decide(created.promotion_id, _press(PromotionAction.PROMOTE))
    assert "developer 역할은 다음 작업부터" in result.detail
    assert langfuse.production("developer") == 2


async def test_disabled_feature_is_reported(harness: Harness, claude: CoreClient) -> None:
    lease = await _leased(harness, claude)
    with pytest.raises(CoreApiError, match="not enabled"):
        await claude.propose_prompt(lease.task_id, _request(lease.lease_id))


def test_owner_comes_from_the_connectors_config(tmp_path: Path) -> None:
    config = tmp_path / "slack.yaml"
    config.write_text("owner: U0OWNER\nroutes:\n  direct_message: assistant\n")
    assert load_owner(config) == "U0OWNER"
    config.write_text("routes: {}\n")
    with pytest.raises(ConfigurationError, match="no 'owner'"):
        load_owner(config)


async def test_validation_errors_are_domain_errors(
    harness: Harness, prompts: PromptService, langfuse: FakeLangfuse, claude: CoreClient
) -> None:
    lease = await _leased(harness, claude)
    with pytest.raises(InvalidRequestError):
        await prompts.propose(lease.task_id, _request(lease.lease_id, text="You are enikk."))
    harness.catalog.put(agent("assistant"))
    with pytest.raises(PermissionDeniedError):
        await prompts.propose(lease.task_id, _request(lease.lease_id))


async def test_read_prompt_returns_a_role_prompt_without_writing(
    harness: Harness, prompts: PromptService, langfuse: FakeLangfuse, claude: CoreClient
) -> None:
    langfuse.fail_writes = True
    lease = await _leased(harness, claude)
    current = await claude.read_prompt(
        lease.task_id, PromptReadRequest(lease_id=lease.lease_id, prompt="assistant")
    )
    assert (current.version, current.text) == (3, "You are enikk.")
    assert current.config == {"model": "claude-a", "effort": "low"}
    assert PRODUCTION in current.labels
    older = await claude.read_prompt(
        lease.task_id, PromptReadRequest(lease_id=lease.lease_id, prompt="assistant", version=1)
    )
    assert older.text == "v1 text"
    developer = await claude.read_prompt(
        lease.task_id, PromptReadRequest(lease_id=lease.lease_id, prompt="developer")
    )
    assert developer.text == "dev text"
    with pytest.raises(CoreApiError, match="not the prompt of any role"):
        await claude.read_prompt(
            lease.task_id, PromptReadRequest(lease_id=lease.lease_id, prompt="other")
        )
    with pytest.raises(CoreApiError, match="has no label candidate"):
        await claude.read_prompt(
            lease.task_id,
            PromptReadRequest(lease_id=lease.lease_id, prompt="assistant", label="candidate"),
        )
    assert langfuse.created == []
    assert langfuse.production_moves == []


async def test_read_prompt_needs_the_grant_and_the_lease(
    harness: Harness, prompts: PromptService, claude: CoreClient
) -> None:
    lease = await _leased(harness, claude)
    with pytest.raises(CoreApiError):
        await claude.read_prompt(
            lease.task_id, PromptReadRequest(lease_id=uuid.uuid4(), prompt="assistant")
        )
    async with harness.client("slack") as slack:
        with pytest.raises(CoreApiError, match="may not call this endpoint"):
            await slack.read_prompt(
                lease.task_id, PromptReadRequest(lease_id=lease.lease_id, prompt="assistant")
            )
    harness.catalog.put(agent("assistant"))
    with pytest.raises(CoreApiError, match="may not create prompt candidates"):
        await claude.read_prompt(
            lease.task_id, PromptReadRequest(lease_id=lease.lease_id, prompt="assistant")
        )
