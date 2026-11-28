from __future__ import annotations

from pathlib import Path

import pytest
import yaml

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
"""


def test_repository_agent_definitions_are_valid() -> None:
    catalog = YamlAgentCatalog.load(REPO_ROOT / "agents")
    assert "enikk" in catalog.names()
    enikk = catalog.get("enikk")
    assert enikk is not None
    assert enikk.engine is Engine.CLAUDE
    assert enikk.prompt.name == "enikk"


def test_threads_of_the_former_name_run_as_enikk() -> None:
    catalog = YamlAgentCatalog.load(REPO_ROOT / "agents")
    assert "assistant" not in catalog.names()
    assert catalog.get("assistant") is catalog.get("enikk")


def test_routes_name_an_agent() -> None:
    catalog = YamlAgentCatalog.load(REPO_ROOT / "agents")
    slack = yaml.safe_load((REPO_ROOT / "config" / "slack.yaml").read_text())
    for agent in slack["routes"].values():
        assert agent in catalog.names(), agent
    for channel in slack.get("agent_channels", []):
        assert channel["agent"] in catalog.names(), channel


def test_aliases_may_not_clash(tmp_path: Path) -> None:
    (tmp_path / "one.yaml").write_text(VALID.format(name="one") + "aliases: [old]\n")
    (tmp_path / "two.yaml").write_text(VALID.format(name="two") + "aliases: [old]\n")
    with pytest.raises(ConfigurationError, match="alias 'old'"):
        YamlAgentCatalog.load(tmp_path)
    (tmp_path / "two.yaml").write_text(VALID.format(name="two") + "aliases: [one]\n")
    with pytest.raises(ConfigurationError, match="alias 'one'"):
        YamlAgentCatalog.load(tmp_path)
    (tmp_path / "two.yaml").write_text(VALID.format(name="two") + "aliases: [two]\n")
    with pytest.raises(ConfigurationError, match="aliases must differ"):
        YamlAgentCatalog.load(tmp_path)


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
    assert set(catalog.names()) >= {"enikk", *WITHOUT_TOOLS}
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


def test_enikk_hands_work_to_rita_and_arcana() -> None:
    catalog = YamlAgentCatalog.load(REPO_ROOT / "agents")
    enikk = catalog.get("enikk")
    assert enikk is not None
    assert enikk.delegates == ("rita", "arcana")


def test_rita_and_arcana_schedule_only_themselves() -> None:
    """ADR-0052: they have the scheduler, and no schedule lends them another's tools."""
    from enikk_core_domain.schedules import may_manage

    catalog = YamlAgentCatalog.load(REPO_ROOT / "agents")
    for name in ("rita", "arcana"):
        definition = catalog.get(name)
        assert definition is not None
        assert "scheduler" in {grant.server for grant in definition.tools.mcp}, name
        others = [other for other in catalog.names() if other not in {name, "summarizer"}]
        assert not [o for o in others if may_manage(definition, catalog.get(o))], name
    enikk = catalog.get("enikk")
    assert all(may_manage(enikk, catalog.get(name)) for name in catalog.names())


def test_only_arcana_writes_the_owners_unpublished_notes() -> None:
    from enikk_contracts.agents import PlatformTool

    catalog = YamlAgentCatalog.load(REPO_ROOT / "agents")
    granted = {
        name
        for name in catalog.names()
        if PlatformTool.VAULT_PRIVATE in catalog.get(name).tools.platform  # type: ignore[union-attr]
    }
    assert granted == {"arcana"}


@pytest.mark.parametrize(
    ("target", "fragment"),
    [
        ("ghost", "is not an agent"),
        ("middle", "delegate to no one"),
    ],
)
def test_delegates_must_be_plain_shared_agents(tmp_path: Path, target: str, fragment: str) -> None:
    (tmp_path / "good.yaml").write_text(VALID.format(name="good"))
    (tmp_path / "middle.yaml").write_text(VALID.format(name="middle") + "delegates: [good]\n")
    (tmp_path / "top.yaml").write_text(VALID.format(name="top") + f"delegates: [{target}]\n")
    with pytest.raises(ConfigurationError, match=fragment):
        YamlAgentCatalog.load(tmp_path)


def test_catalog_reports_every_problem(tmp_path: Path) -> None:
    (tmp_path / "good.yaml").write_text(VALID.format(name="good"))
    (tmp_path / "delegating.yaml").write_text(
        VALID.format(name="delegating") + "delegates: [delegating]\n"
    )
    (tmp_path / "renamed.yaml").write_text(VALID.format(name="other"))
    (tmp_path / "broken.yaml").write_text("name: broken\nengine: [")
    (tmp_path / "extra.yaml").write_text(VALID.format(name="extra") + "x: 1\n")
    with pytest.raises(ConfigurationError) as caught:
        YamlAgentCatalog.load(tmp_path)
    message = str(caught.value)
    for fragment in ("delegating.yaml", "must match the file name", "broken.yaml", "extra.yaml"):
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


def test_scheduler_role_binding_has_no_connector_or_engine() -> None:
    binding = RoleBindingSettings(
        namespace="enikk", service_account="enikk-scheduler", role="scheduler"
    ).to_binding()
    assert binding.role.kind == "scheduler"
    with pytest.raises(ValueError, match="engine"):
        RoleBindingSettings(
            namespace="enikk", service_account="x", role="scheduler", engine=Engine.CLAUDE
        )
