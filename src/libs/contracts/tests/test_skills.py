"""Skill config (ADR-0037 §2)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from enikk_contracts.prompts import PromptConfigChange
from enikk_contracts.skills import (
    Capability,
    declared_requires,
    is_skill,
    skill_config,
    valid_skill_name,
    widened,
)


def test_a_valid_config() -> None:
    config = skill_config(
        {
            "model": "claude-opus-5-5",
            "effort": "high",
            "description": "코드를 바꾸고 PR 을 만들 때",
            "requires": ["repo.push", "pr.write"],
        }
    )
    assert config.requires == (Capability.REPO_PUSH, Capability.PR_WRITE)
    assert skill_config({"model": "m", "description": "d"}).requires == ()


@pytest.mark.parametrize(
    ("config", "problem"),
    [
        ({"model": "m", "description": "d", "requires": ["cluster.write"]}, "requires"),
        ({"model": "m", "description": "d", "requires": "repo.push"}, "requires"),
        ({"model": "m", "description": "d", "requires": ["pr.write", "pr.write"]}, "twice"),
        ({"model": "m"}, "description"),
        ({"model": "m", "description": "두 줄\n설명"}, "one line"),
        ({"description": "d"}, "model"),
        ({"model": "m", "description": "d", "tools": ["Bash"]}, "tools"),
    ],
)
def test_invalid_configs_are_refused(config: dict[str, object], problem: str) -> None:
    with pytest.raises(ValueError, match=problem):
        skill_config(config)


def test_widened_lists_only_new_capabilities() -> None:
    before = (Capability.PR_WRITE,)
    after = (Capability.REPO_PUSH, Capability.PR_WRITE)
    assert widened(before, after) == (Capability.REPO_PUSH,)
    assert widened(after, before) == ()


def test_declared_requires_ignores_unknown_values() -> None:
    assert declared_requires({"requires": ["pr.write", "x", 3]}) == (Capability.PR_WRITE,)
    assert declared_requires({}) == ()


def test_skill_names() -> None:
    assert is_skill("skill-developer")
    assert valid_skill_name("skill-developer")
    assert is_skill("skill-Bad")
    assert not valid_skill_name("skill-Bad")
    assert not is_skill("assistant")


def test_a_config_change_refuses_unknown_requires() -> None:
    with pytest.raises(ValidationError):
        PromptConfigChange.model_validate_json('{"requires": ["cluster.write"]}')
    change = PromptConfigChange.model_validate_json('{"requires": ["prompt.propose"]}')
    assert change.requires == (Capability.PROMPT_PROPOSE,)
