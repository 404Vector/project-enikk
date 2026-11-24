"""Agent definition catalog loaded from ``agents/*.yaml`` (ADR-0005, ADR-0012 §6)."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pydantic_core
import yaml
from pydantic import ValidationError

from enikk_contracts.agents import AgentDefinition
from enikk_kernel.errors import ConfigurationError

if TYPE_CHECKING:
    from pathlib import Path


class YamlAgentCatalog:
    """Immutable catalog. Loading fails if any definition is invalid."""

    def __init__(self, definitions: tuple[AgentDefinition, ...]) -> None:
        self._by_name = {d.name: d for d in definitions}

    @classmethod
    def load(cls, directory: Path) -> YamlAgentCatalog:
        if not directory.is_dir():
            raise ConfigurationError(f"agent definition directory not found: {directory}")
        problems: list[str] = []
        definitions: list[AgentDefinition] = []
        for path in sorted(directory.glob("*.yaml")):
            try:
                definition = parse_definition(path.read_text(encoding="utf-8"))
            except (yaml.YAMLError, ValidationError) as exc:
                problems.append(f"{path.name}: {exc}")
                continue
            if definition.name != path.stem:
                problems.append(f"{path.name}: name '{definition.name}' must match the file name")
            definitions.append(definition)

        if problems:
            raise ConfigurationError("invalid agent definitions:\n" + "\n".join(problems))
        return cls(tuple(definitions))

    def get(self, name: str) -> AgentDefinition | None:
        return self._by_name.get(name)

    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._by_name))


def parse_definition(text: str) -> AgentDefinition:
    """Parse one YAML definition with the same strict rules as JSON input."""
    document = yaml.safe_load(text)
    return AgentDefinition.model_validate_json(pydantic_core.to_json(document))
