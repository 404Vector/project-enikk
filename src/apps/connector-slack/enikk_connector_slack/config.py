"""Slack routing configuration, ``config/slack.yaml`` (I-15, ADR-0018)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated

import pydantic_core
import yaml
from pydantic import StringConstraints, ValidationError

from enikk_contracts.common import AgentName
from enikk_kernel.errors import ConfigurationError
from enikk_kernel.models import FrozenModel

if TYPE_CHECKING:
    from pathlib import Path

SlackUserId = Annotated[str, StringConstraints(pattern=r"^[UW][A-Z0-9]{2,30}$")]


class Routes(FrozenModel):
    """Agent for a new conversation, by how it started. Follow-ups keep their agent."""

    direct_message: AgentName
    mention: AgentName


class SlackConfig(FrozenModel):
    owner: SlackUserId
    """The only Slack member whose messages are handled (I-01)."""
    routes: Routes

    @classmethod
    def parse(cls, text: str) -> SlackConfig:
        return cls.model_validate_json(pydantic_core.to_json(yaml.safe_load(text)))

    @classmethod
    def load(cls, path: Path) -> SlackConfig:
        try:
            return cls.parse(path.read_text(encoding="utf-8"))
        except OSError as exc:
            raise ConfigurationError(f"cannot read Slack config {path}: {exc}") from exc
        except (yaml.YAMLError, ValidationError) as exc:
            raise ConfigurationError(f"invalid Slack config {path}: {exc}") from exc
