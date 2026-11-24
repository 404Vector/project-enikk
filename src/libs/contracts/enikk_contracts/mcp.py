"""Core ↔ gateway: the public keys of the tasks' MCP tokens (ADR-0039 §2)."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field

from enikk_contracts.base import Contract

TOKEN_ISSUER = "enikk-core"  # noqa: S105 - the iss claim, not a secret
TOKEN_AUDIENCE = "enikk-agentgateway"  # noqa: S105 - the aud claim


class McpJwk(Contract):
    """One EC P-256 public key, as a JSON Web Key."""

    kty: Literal["EC"] = "EC"
    crv: Literal["P-256"] = "P-256"
    alg: Literal["ES256"] = "ES256"
    use: Literal["sig"] = "sig"
    kid: Annotated[str, Field(min_length=1, max_length=100)]
    x: Annotated[str, Field(min_length=1, max_length=100)]
    y: Annotated[str, Field(min_length=1, max_length=100)]


class McpJwks(Contract):
    """Empty while core has no signing key."""

    keys: tuple[McpJwk, ...] = ()
