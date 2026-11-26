"""Tool settings: Langfuse prompts named ``config-*`` that a tool server reads at run
time, so the owner changes them by promoting a candidate instead of a commit
(ADR-0046). The text is a note for people; the config holds the values. Only the
names listed in ``TOOL_CONFIGS`` exist, each with its own contract."""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Final

import pydantic_core
from pydantic import Field, ValidationError

from enikk_contracts.base import Contract

if TYPE_CHECKING:
    from collections.abc import Mapping

TOOL_CONFIG_PREFIX: Final = "config-"
IMAGEGEN_CONFIG: Final = "config-imagegen"


class ImagegenConfig(Contract):
    """The settings of the image tool server. Unknown keys are refused."""

    daily_budget_usd: Annotated[float, Field(ge=0, le=100)]
    """Estimated USD the server may spend per day (Asia/Seoul); 0 stops generation."""


TOOL_CONFIGS: Final[Mapping[str, type[Contract]]] = {IMAGEGEN_CONFIG: ImagegenConfig}


def is_tool_config(prompt: str) -> bool:
    """Whether ``prompt`` is named as a tool setting (it may still be unknown)."""
    return prompt.startswith(TOOL_CONFIG_PREFIX)


def tool_config(prompt: str, config: Mapping[str, object]) -> Contract:
    """``config`` checked against the contract of ``prompt``; ``ValueError`` saying what
    is wrong, or that no tool setting has that name."""
    contract = TOOL_CONFIGS.get(prompt)
    if contract is None:
        known = ", ".join(sorted(TOOL_CONFIGS))
        raise ValueError(f"'{prompt}' is not a tool setting; known: {known}")
    try:
        return contract.model_validate_json(pydantic_core.to_json(dict(config)))
    except pydantic_core.PydanticSerializationError as exc:
        raise ValueError(f"invalid config of {prompt} (not JSON)") from exc
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(p) for p in error['loc']) or 'config'}: {error['msg']}"
            for error in exc.errors()
        )
        raise ValueError(f"invalid config of {prompt}: {problems}") from exc
