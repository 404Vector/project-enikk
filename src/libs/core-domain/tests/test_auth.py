from __future__ import annotations

import pytest

from enikk_contracts.common import ConnectorKind, Engine
from enikk_core_domain.auth import (
    ConnectorRole,
    RoleBinding,
    RoleDirectory,
    RuntimeRole,
    ServiceIdentity,
)

SLACK = ServiceIdentity(namespace="enikk", name="enikk-connector-slack")
CLAUDE = ServiceIdentity(namespace="enikk", name="enikk-runtime-claude")


def test_directory_maps_known_identities_only() -> None:
    directory = RoleDirectory(
        (
            RoleBinding(identity=SLACK, role=ConnectorRole(connector=ConnectorKind.SLACK)),
            RoleBinding(identity=CLAUDE, role=RuntimeRole(engine=Engine.CLAUDE)),
        )
    )
    assert directory.role_of(SLACK) == ConnectorRole(connector=ConnectorKind.SLACK)
    assert directory.role_of(CLAUDE) == RuntimeRole(engine=Engine.CLAUDE)
    assert directory.role_of(ServiceIdentity(namespace="enikk", name="default")) is None


def test_directory_rejects_duplicate_bindings() -> None:
    binding = RoleBinding(identity=SLACK, role=ConnectorRole(connector=ConnectorKind.SLACK))
    with pytest.raises(ValueError, match="duplicate"):
        RoleDirectory((binding, binding))
