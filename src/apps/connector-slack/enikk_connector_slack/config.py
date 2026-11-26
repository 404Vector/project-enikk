"""Slack routing configuration, ``config/slack.yaml`` (I-15, ADR-0018)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Final, Self

import pydantic_core
import yaml
from pydantic import Field, StringConstraints, ValidationError, model_validator

from enikk_connector_slack.marks import EmojiName, Reactions
from enikk_contracts.common import AgentName
from enikk_kernel.errors import ConfigurationError
from enikk_kernel.models import FrozenModel

if TYPE_CHECKING:
    from pathlib import Path

SlackUserId = Annotated[str, StringConstraints(pattern=r"^[UW][A-Z0-9]{2,30}$")]
SlackChannelId = Annotated[str, StringConstraints(pattern=r"^[CG][A-Z0-9]{2,30}$")]

DEFAULT_DEPLOY_NOTIFY_CHANNEL: Final = "C0CKT1ZNZ43"  # #release
DEFAULT_APPROVAL_CHANNEL: Final = "C0CL7HZ5UQ1"


class Routes(FrozenModel):
    """Agent for a new conversation, by how it started. Follow-ups keep their agent."""

    direct_message: AgentName
    mention: AgentName


DisplayName = Annotated[str, StringConstraints(min_length=1, max_length=80, strip_whitespace=True)]


class Persona(FrozenModel):
    """The name and icon an agent's messages show in Slack (ADR-0045)."""

    name: DisplayName
    icon_emoji: EmojiName | None = None
    """A custom emoji of the workspace, without colons. None: the app's own icon."""


class AgentChannel(FrozenModel):
    """A channel of one agent: the owner's messages there go to it (ADR-0045)."""

    channel: SlackChannelId
    agent: AgentName
    persona: Persona


class SlackConfig(FrozenModel):
    owner: SlackUserId
    """The only Slack member whose messages are handled (I-01)."""
    routes: Routes
    deploy_notify_channel: SlackChannelId = DEFAULT_DEPLOY_NOTIFY_CHANNEL
    """Channel for deployment, image build and rollback notices (ADR-0026)."""
    approval_channel: SlackChannelId = DEFAULT_APPROVAL_CHANNEL
    """Channel for approval requests: pull requests and prompt promotions (ADR-0036)."""
    reactions: Reactions = Reactions()
    """Custom emoji for the marks on the owner's messages (ADR-0042)."""
    agent_channels: Annotated[tuple[AgentChannel, ...], Field(max_length=50)] = ()
    """Channels that belong to one agent each (ADR-0045)."""

    @model_validator(mode="after")
    def _distinct_channels(self) -> Self:
        channels = [c.channel for c in self.agent_channels]
        if len(channels) != len(set(channels)):
            raise ValueError("each agent channel may appear only once")
        shared = {self.deploy_notify_channel, self.approval_channel} & set(channels)
        if shared:
            raise ValueError(f"agent channels may not be a notice channel: {sorted(shared)}")
        return self

    def agent_channel(self, channel: str) -> AgentChannel | None:
        """The agent channel ``channel``, if it is one."""
        return next((c for c in self.agent_channels if c.channel == channel), None)

    def with_deploy_channel(self, channel: str | None) -> SlackConfig:
        """This config with ``channel`` for deployment notices, if one is given."""
        return self._with("deploy_notify_channel", channel)

    def with_approval_channel(self, channel: str | None) -> SlackConfig:
        """This config with ``channel`` for approval requests, if one is given."""
        return self._with("approval_channel", channel)

    def _with(self, field: str, channel: str | None) -> SlackConfig:
        if channel is None:
            return self
        try:
            return SlackConfig.model_validate({**dict(self), field: channel})
        except ValidationError as exc:
            raise ConfigurationError(f"invalid {field.replace('_', ' ')}: {exc}") from exc

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
