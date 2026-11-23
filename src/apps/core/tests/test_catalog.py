from __future__ import annotations

from pathlib import Path

import pytest

from enikk_contracts.common import Engine
from enikk_core.catalog import YamlAgentCatalog
from enikk_core.settings import CoreSettings, RoleBindingSettings
from enikk_kernel.errors import ConfigurationError

REPO_ROOT = Path(__file__).resolve().parents[4]

VALID = """\
name: {name}
description: test agent
engine: claude
prompt: {{name: {name}, label: production}}
delegates: {delegates}
"""


def test_repository_agent_definitions_are_valid() -> None:
    catalog = YamlAgentCatalog.load(REPO_ROOT / "agents")
    assert "assistant" in catalog.names()
    assistant = catalog.get("assistant")
    assert assistant is not None
    assert assistant.engine is Engine.CLAUDE


# The owner's decision (ADR-0033): every role has the SDK's file, search, command,
# web and work-tracking tools, under the names Claude Code 2.1.330 uses.
EVERY_ROLE_HAS: frozenset[str] = frozenset(
    {
        "Read",
        "Write",
        "Edit",
        "NotebookEdit",
        "Glob",
        "Grep",
        "Bash",
        "Monitor",
        "TaskStop",
        "WebSearch",
        "WebFetch",
        "TaskCreate",
        "TaskGet",
        "TaskList",
        "TaskUpdate",
        "Task",
    }
)


# The owner's decision (ADR-0035): the summarizer reads untrusted conversations and has
# no tools at all.
WITHOUT_TOOLS: frozenset[str] = frozenset({"summarizer"})


def test_every_role_is_granted_the_sdk_tools() -> None:
    catalog = YamlAgentCatalog.load(REPO_ROOT / "agents")
    assert set(catalog.names()) >= {"assistant", "developer", *WITHOUT_TOOLS}
    for name in set(catalog.names()) - WITHOUT_TOOLS:
        definition = catalog.get(name)
        assert definition is not None
        assert set(definition.tools.builtin) == EVERY_ROLE_HAS, name


def test_the_summarizer_has_no_tools() -> None:
    catalog = YamlAgentCatalog.load(REPO_ROOT / "agents")
    for name in WITHOUT_TOOLS:
        definition = catalog.get(name)
        assert definition is not None
        tools = definition.tools
        assert (tools.builtin, tools.mcp, tools.platform) == ((), (), ()), name
        assert tools.platform_defaults is False, name
        assert definition.delegates == (), name


def test_catalog_reports_every_problem(tmp_path: Path) -> None:
    (tmp_path / "good.yaml").write_text(VALID.format(name="good", delegates="[ghost]"))
    (tmp_path / "renamed.yaml").write_text(VALID.format(name="other", delegates="[]"))
    (tmp_path / "broken.yaml").write_text("name: broken\nengine: [")
    (tmp_path / "extra.yaml").write_text(VALID.format(name="extra", delegates="[]") + "x: 1\n")
    with pytest.raises(ConfigurationError) as caught:
        YamlAgentCatalog.load(tmp_path)
    message = str(caught.value)
    for fragment in ("ghost", "must match the file name", "broken.yaml", "extra.yaml"):
        assert fragment in message


def test_missing_directory_is_a_configuration_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError):
        YamlAgentCatalog.load(tmp_path / "missing")


def test_role_bindings_parse_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(
        "ENIKK_CORE_ROLE_BINDINGS",
        '[{"namespace": "enikk", "service_account": "enikk-runtime-claude",'
        ' "role": "runtime", "engine": "claude"}]',
    )
    monkeypatch.setenv("ENIKK_CORE_DATABASE__PORT", "6543")
    settings = CoreSettings()
    assert settings.database.port == 6543
    binding = settings.role_bindings[0].to_binding()
    assert binding.identity.name == "enikk-runtime-claude"


def test_role_binding_requires_matching_fields() -> None:
    with pytest.raises(ValueError, match="engine"):
        RoleBindingSettings(namespace="enikk", service_account="x", role="runtime")
