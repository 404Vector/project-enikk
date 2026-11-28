"""Agent definition catalog loaded from ``agents/*.yaml`` (ADR-0005, ADR-0012 §6)."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pydantic_core
import yaml
from pydantic import ValidationError

from enikk_contracts.agents import AgentDefinition, Execution
from enikk_kernel.errors import ConfigurationError

if TYPE_CHECKING:
    from pathlib import Path


class YamlAgentCatalog:
    """Immutable catalog. Loading fails if any definition is invalid. A definition is
    found by its name or by one of its aliases (former names)."""

    def __init__(self, definitions: tuple[AgentDefinition, ...]) -> None:
        self._by_name = {d.name: d for d in definitions}
        self._by_alias: dict[str, AgentDefinition] = {}
        for definition in definitions:
            for alias in definition.aliases:
                if alias in self._by_name or alias in self._by_alias:
                    raise ConfigurationError(
                        f"alias '{alias}' of '{definition.name}' is already an agent's name "
                        "or alias"
                    )
                self._by_alias[alias] = definition
        for definition in definitions:
            for target in definition.delegates:
                found = self._by_name.get(target)
                if found is None:
                    raise ConfigurationError(
                        f"'{definition.name}' delegates to '{target}', which is not an agent"
                    )
                if found.execution is not Execution.SHARED or found.delegates:
                    raise ConfigurationError(
                        f"'{definition.name}' delegates to '{target}', which must run on the "
                        "shared runtime and delegate to no one (ADR-0047)"
                    )
            for target in definition.asks:
                found = self._by_name.get(target)
                if found is None:
                    raise ConfigurationError(
                        f"'{definition.name}' asks '{target}', which is not an agent"
                    )
                if found.execution is not Execution.SHARED:
                    raise ConfigurationError(
                        f"'{definition.name}' asks '{target}', which must run on the shared "
                        "runtime (ADR-0054)"
                    )

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
        return self._by_name.get(name) or self._by_alias.get(name)

    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._by_name))


def parse_definition(text: str) -> AgentDefinition:
    """Parse one YAML definition with the same strict rules as JSON input."""
    document = yaml.safe_load(text)
    return AgentDefinition.model_validate_json(pydantic_core.to_json(document))
