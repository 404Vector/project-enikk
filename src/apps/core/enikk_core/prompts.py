"""Prompt candidates and the owner's promotion in Slack (ADR-0032, I-36).

- A role granted ``prompt-candidates`` asks core for a candidate. Core checks it, writes
  a new version labelled ``candidate`` alone to Langfuse, records the request and queues
  a DM for the owner.
- The owner's [승격] moves ``production`` to the candidate, only while ``production`` is
  still the version the request was made against. [거절] changes nothing in Langfuse.
  [되돌리기] moves ``production`` back, only while the candidate is still production.
- Every press runs under the request's row lock, so a repeated press does nothing.
"""

from __future__ import annotations

import logging
from pathlib import Path  # noqa: TC003 - used at runtime
from typing import TYPE_CHECKING, Final

import yaml

from enikk_contracts.agents import Execution, PlatformTool
from enikk_contracts.common import ConnectorKind
from enikk_contracts.outbox import OutboundKind
from enikk_contracts.prompts import (
    BASE_PROMPT,
    MAX_DIFF_CHARS,
    PromotionAction,
    PromotionLinked,
    PromotionRequested,
    PromotionResult,
    PromotionStatus,
    PromptCandidateCreated,
    PromptRead,
)
from enikk_contracts.skills import is_skill
from enikk_core.wakeups import PROMOTION_STATES, promotion_event_text
from enikk_core_domain.errors import (
    InvalidRequestError,
    NotFoundError,
    PermissionDeniedError,
)
from enikk_core_domain.model import Notice, OutboxMessage
from enikk_core_domain.prompts import (
    PRODUCTION,
    PromptPromotion,
    check_candidate,
    roles_using,
)
from enikk_kernel.errors import ConfigurationError

if TYPE_CHECKING:
    import uuid

    from enikk_contracts.agents import AgentDefinition
    from enikk_contracts.prompts import (
        PromotionDecision,
        PromotionPosted,
        PromptCandidateRequest,
        PromptReadRequest,
    )
    from enikk_core.wakeups import SessionWaker
    from enikk_core_domain.model import Session, Task
    from enikk_core_domain.ports import AgentCatalog, UnitOfWork, UnitOfWorkFactory
    from enikk_core_domain.prompts import PromptRegistry
    from enikk_kernel.clock import Clock
    from enikk_kernel.ids import IdGenerator

SHARED_CACHE_SECONDS: Final = 60
"""How long a shared runtime keeps a prompt before asking Langfuse again (ADR-0016)."""
TRUNCATED: Final = "\n…(diff 가 길어 잘렸습니다)"

_log = logging.getLogger(__name__)


def load_owner(slack_config: Path) -> str:
    """The owner's Slack user ID from the connector's ``config/slack.yaml`` (I-01)."""
    try:
        loaded = yaml.safe_load(slack_config.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigurationError(f"cannot read the owner from {slack_config}: {exc}") from exc
    owner = loaded.get("owner") if isinstance(loaded, dict) else None
    if not isinstance(owner, str) or not owner:
        raise ConfigurationError(f"{slack_config} has no 'owner'")
    return owner


class PromptService:
    def __init__(
        self,
        *,
        uow: UnitOfWorkFactory,
        catalog: AgentCatalog,
        registry: PromptRegistry,
        owner: str,
        clock: Clock,
        ids: IdGenerator,
        waker: SessionWaker | None = None,
    ) -> None:
        self._uow = uow
        self._catalog = catalog
        self._registry = registry
        self._owner = owner
        self._clock = clock
        self._ids = ids
        self._waker = waker

    # ------------------------------------------------------------ reading (ADR-0032 §8)

    async def read(self, task_id: uuid.UUID, request: PromptReadRequest) -> PromptRead:
        """One version of a role's prompt, for a role that may propose candidates.
        Read-only; Langfuse's keys stay in core (I-36)."""
        await self._granted(task_id, request.prompt)
        label = None if request.version is not None else (request.label or PRODUCTION)
        stored = await self._registry.get(request.prompt, label=label, version=request.version)
        if stored is None:
            selector = f"label {label}" if label is not None else f"version {request.version}"
            raise NotFoundError(f"prompt '{request.prompt}' has no {selector}")
        return PromptRead(
            prompt=stored.name,
            version=stored.version,
            labels=stored.labels,
            type=stored.type,
            text=stored.text,
            config=dict(stored.config),
        )

    async def _granted(
        self, task_id: uuid.UUID, prompt: str
    ) -> tuple[Task, Session, tuple[str, ...]]:
        """The task, its session and the roles using ``prompt``; refuse unless the task's
        role may handle prompts and ``prompt`` is a role's prompt, the base prompt or a
        skill (ADR-0037 §1, §2)."""
        task, session = await self._origin(task_id)
        definition = self._catalog.get(task.agent)
        if definition is None or PlatformTool.PROMPT_CANDIDATES not in definition.tools.platform:
            raise PermissionDeniedError(f"role '{task.agent}' may not create prompt candidates")
        roles = roles_using(prompt, self._definitions())
        if not roles and not is_skill(prompt) and prompt != BASE_PROMPT:
            known = ", ".join(sorted({d.prompt.name for d in self._definitions()}))
            raise InvalidRequestError(
                f"'{prompt}' is not the prompt of any role, the base prompt "
                f"({BASE_PROMPT}) nor a skill (skill-*); roles use: {known}"
            )
        return task, session, roles

    # ------------------------------------------------------------ candidates

    async def propose(
        self, task_id: uuid.UUID, request: PromptCandidateRequest
    ) -> PromptCandidateCreated:
        """Create a candidate version and ask the owner. Never touches ``production``."""
        task, session, roles = await self._granted(task_id, request.prompt)
        production = await self._registry.get(request.prompt, label=PRODUCTION)
        candidate = check_candidate(request.prompt, production, request.text, request.config)
        created = await self._registry.create_candidate(
            request.prompt, candidate.text, candidate.config, request.reason
        )
        now = self._clock.now()
        promotion = PromptPromotion(
            id=self._ids.new(),
            prompt=request.prompt,
            production_version=None if production is None else production.version,
            candidate_version=created.version,
            reason=request.reason,
            task_id=task.id,
            created_at=now,
        )
        notice = PromotionRequested(
            promotion_id=promotion.id,
            prompt=promotion.prompt,
            roles=roles,
            production_version=promotion.production_version,
            candidate_version=promotion.candidate_version,
            reason=promotion.reason,
            text_diff=_fit(candidate.text_diff, MAX_DIFF_CHARS),
            config_diff=_fit(candidate.config_diff, 20_000),
            requires_added=candidate.requires_added,
            origin=session.outbox_conversation,
        )
        async with self._uow() as uow:
            await uow.prompt_promotions.add(promotion)
            await uow.notices.add(Notice(id=self._ids.new(), body=notice, created_at=now))
            await uow.commit()
        _log.info(
            "prompt %s: candidate v%d proposed by task %s (production %s)",
            promotion.prompt,
            promotion.candidate_version,
            task.id,
            promotion.production_version,
        )
        return PromptCandidateCreated(
            promotion_id=promotion.id,
            prompt=promotion.prompt,
            candidate_version=promotion.candidate_version,
            production_version=promotion.production_version,
        )

    async def posted(self, promotion_id: uuid.UUID, posted: PromotionPosted) -> PromotionLinked:
        """The request is posted (approval channel, or the owner's DM if that failed,
        ADR-0036): tell the asking thread, with its link."""
        async with self._uow() as uow:
            promotion = await uow.prompt_promotions.get_for_update(promotion_id)
            if promotion is None:
                raise NotFoundError(f"prompt promotion {promotion_id} not found")
            where = f": {posted.link}" if posted.link else "."
            told = await self._tell(
                uow,
                promotion,
                f"프롬프트 `{promotion.prompt}` 후보 v{promotion.candidate_version} 의 "
                f"승격 요청을 보냈습니다{where}",
            )
            await uow.commit()
        return PromotionLinked(told_origin=told)

    # ------------------------------------------------------------ the owner's decision

    async def decide(self, promotion_id: uuid.UUID, decision: PromotionDecision) -> PromotionResult:
        if decision.decided_by != self._owner:
            raise PermissionDeniedError("only the owner promotes, rejects or reverts prompts")
        async with self._uow() as uow:
            promotion = await uow.prompt_promotions.get_for_update(promotion_id)
            if promotion is None:
                raise NotFoundError(f"prompt promotion {promotion_id} not found")
            if decision.action is PromotionAction.REVERT:
                result = await self._revert(uow, promotion, decision.decided_by)
            else:
                result = await self._decide(uow, promotion, decision)
            await uow.commit()
        _log.info(
            "prompt %s v%d: %s by the owner -> %s%s",
            promotion.prompt,
            promotion.candidate_version,
            decision.action,
            result.status,
            "" if result.changed else " (unchanged)",
        )
        return result

    async def _decide(
        self, uow: UnitOfWork, promotion: PromptPromotion, decision: PromotionDecision
    ) -> PromotionResult:
        if promotion.status is not PromotionStatus.REQUESTED:
            return _result(promotion, changed=False, detail="이미 처리된 요청입니다.")
        now = self._clock.now()
        by = decision.decided_by
        name = f"`{promotion.prompt}` v{promotion.candidate_version}"
        if decision.action is PromotionAction.REJECT:
            rejected = promotion.decide(PromotionStatus.REJECTED, now, by)
            await uow.prompt_promotions.update(rejected)
            await self._tell(
                uow,
                rejected,
                f"프롬프트 {name} 승격 요청이 거절됐습니다. "
                "Langfuse 는 바뀌지 않았고 후보 버전은 남아 있습니다.",
            )
            return _result(rejected, changed=True)
        current = await self._registry.get(promotion.prompt, label=PRODUCTION)
        current_version = None if current is None else current.version
        if current_version != promotion.production_version:
            stale = promotion.decide(PromotionStatus.STALE, now, by)
            await uow.prompt_promotions.update(stale)
            detail = (
                f"요청 뒤 production 이 {_version(promotion.production_version)} 에서 "
                f"{_version(current_version)} 로 바뀌어 승격하지 않았습니다. 다시 요청해 주세요."
            )
            await self._tell(uow, stale, f"프롬프트 {name}: {detail}")
            return _result(stale, changed=True, detail=detail)
        await self._registry.set_production(promotion.prompt, promotion.candidate_version)
        promoted = promotion.decide(PromotionStatus.PROMOTED, now, by)
        await uow.prompt_promotions.update(promoted)
        effective = self._effective(promotion.prompt)
        await self._tell(
            uow,
            promoted,
            f"프롬프트 {name} 을 production 으로 승격했습니다 "
            f"({_version(promotion.production_version)} → v{promotion.candidate_version}). "
            f"반영: {effective}",
        )
        return _result(promoted, changed=True, detail=f"반영: {effective}")

    async def _revert(
        self, uow: UnitOfWork, promotion: PromptPromotion, by: str
    ) -> PromotionResult:
        if promotion.status is not PromotionStatus.PROMOTED:
            detail = (
                "이미 되돌렸습니다."
                if promotion.status is PromotionStatus.REVERTED
                else "승격된 요청만 되돌릴 수 있습니다."
            )
            return _result(promotion, changed=False, detail=detail)
        previous = promotion.production_version
        if previous is None:
            return _result(promotion, changed=False, detail="승격 전 production 이 없었습니다.")
        current = await self._registry.get(promotion.prompt, label=PRODUCTION)
        current_version = None if current is None else current.version
        if current_version != promotion.candidate_version:
            detail = (
                f"지금 production 은 {_version(current_version)} 라서 되돌리지 않았습니다. "
                "그 사이 다른 버전이 승격됐습니다."
            )
            return _result(promotion, changed=False, detail=detail)
        await self._registry.set_production(promotion.prompt, previous)
        reverted = promotion.revert(self._clock.now(), by)
        await uow.prompt_promotions.update(reverted)
        effective = self._effective(promotion.prompt)
        await self._tell(
            uow,
            reverted,
            f"프롬프트 `{promotion.prompt}` 를 v{previous} 로 되돌렸습니다 "
            f"(v{promotion.candidate_version} → v{previous}). 반영: {effective}",
        )
        return _result(reverted, changed=True, detail=f"반영: {effective}")

    # ------------------------------------------------------------ helpers

    def _definitions(self) -> list[AgentDefinition]:
        found = (self._catalog.get(name) for name in self._catalog.names())
        return [d for d in found if d is not None]

    def _effective(self, prompt: str) -> str:
        """When each role using ``prompt`` starts using the new production."""
        if is_skill(prompt):
            return f"스킬은 최대 {SHARED_CACHE_SECONDS}초 안에, 다음에 불러올 때부터"
        parts = []
        for definition in self._definitions():
            if definition.prompt.name != prompt:
                continue
            if definition.execution is Execution.ISOLATED:
                parts.append(f"{definition.name} 역할은 다음 작업부터")
            else:
                parts.append(f"{definition.name} 역할은 최대 {SHARED_CACHE_SECONDS}초 안에")
        return ", ".join(parts) or "이 프롬프트를 쓰는 역할이 없습니다"

    async def _origin(self, task_id: uuid.UUID) -> tuple[Task, Session]:
        async with self._uow() as uow:
            task = await uow.tasks.get_for_update(task_id)
            session = None if task is None else await uow.sessions.get(task.session_id)
        if task is None or session is None:
            raise NotFoundError(f"task {task_id} not found")
        if session.conversation.connector is not ConnectorKind.SLACK:
            raise PermissionDeniedError("only work the owner started in Slack may propose prompts")
        return task, session

    async def _tell(self, uow: UnitOfWork, promotion: PromptPromotion, text: str) -> bool:
        """Post ``text`` in the thread that asked for the candidate, and wake its session
        (ADR-0041 §4)."""
        task = await uow.tasks.get_for_update(promotion.task_id)
        session = None if task is None else await uow.sessions.get(task.session_id)
        if task is None or session is None:
            return False
        await uow.outbox.add(
            OutboxMessage(
                id=self._ids.new(),
                task_id=task.id,
                source_event_id=task.source_event_id,
                conversation=session.outbox_conversation,
                kind=OutboundKind.REPLY,
                text=text,
                created_at=self._clock.now(),
            )
        )
        if self._waker is not None and promotion.status in PROMOTION_STATES:
            await self._waker.wake(
                uow,
                session,
                key=f"prompt:{promotion.id}:{promotion.status}",
                text=promotion_event_text(
                    promotion.prompt, promotion.candidate_version, promotion.status, text
                ),
            )
        return True


def _result(promotion: PromptPromotion, *, changed: bool, detail: str = "") -> PromotionResult:
    return PromotionResult(
        status=promotion.status,
        changed=changed,
        prompt=promotion.prompt,
        production_version=promotion.production_version,
        candidate_version=promotion.candidate_version,
        detail=detail,
    )


def _version(version: int | None) -> str:
    return "없음" if version is None else f"v{version}"


def _fit(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - len(TRUNCATED)] + TRUNCATED
