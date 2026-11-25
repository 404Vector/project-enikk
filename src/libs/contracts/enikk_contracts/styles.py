"""Image styles: Langfuse prompts named ``style-*`` that the image tool server puts
before every request made in that style (ADR-0043 §3). The text describes the style;
the config names the image model and says what the style is for."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Annotated, Final

import pydantic_core
from pydantic import StringConstraints, ValidationError

from enikk_contracts.base import Contract

if TYPE_CHECKING:
    from collections.abc import Mapping

STYLE_PREFIX: Final = "style-"
MAX_STYLES: Final = 50
"""At most this many styles are listed."""

STYLE_NAME_PATTERN: Final = r"^style-[a-z0-9][a-z0-9-]{0,62}$"
StyleName = Annotated[str, StringConstraints(pattern=STYLE_NAME_PATTERN)]


class StyleConfig(Contract):
    """The config of a style prompt in Langfuse. Unknown keys and values are refused."""

    model: Annotated[str, StringConstraints(min_length=1, max_length=100)]
    """The image model a request in this style uses unless it names another."""
    description: Annotated[
        str, StringConstraints(min_length=1, max_length=200, strip_whitespace=True)
    ]
    """What the style is for, in one line."""


def is_style(prompt: str) -> bool:
    """Whether ``prompt`` is named as a style (it may still be an invalid name)."""
    return prompt.startswith(STYLE_PREFIX)


def valid_style_name(prompt: str) -> bool:
    return re.fullmatch(STYLE_NAME_PATTERN, prompt) is not None


def style_config(config: Mapping[str, object]) -> StyleConfig:
    """``config`` as a style config; ``ValueError`` saying what is wrong."""
    try:
        return StyleConfig.model_validate_json(pydantic_core.to_json(dict(config)))
    except pydantic_core.PydanticSerializationError as exc:
        raise ValueError("invalid style config (not JSON)") from exc
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(p) for p in error['loc']) or 'config'}: {error['msg']}"
            for error in exc.errors()
        )
        raise ValueError(f"invalid style config: {problems}") from exc
