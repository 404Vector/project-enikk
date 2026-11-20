"""TokenReview wire format (authentication.k8s.io/v1).

Responses come from the Kubernetes API server, which adds fields over time,
so response models ignore unknown fields instead of rejecting them.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class _Request(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True)


class _Response(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")


class TokenReviewSpec(_Request):
    token: str
    audiences: tuple[str, ...]


class TokenReviewRequest(_Request):
    api_version: Literal["authentication.k8s.io/v1"] = Field(
        default="authentication.k8s.io/v1", alias="apiVersion"
    )
    kind: Literal["TokenReview"] = "TokenReview"
    spec: TokenReviewSpec


class UserInfo(_Response):
    username: str = ""


class TokenReviewStatus(_Response):
    authenticated: bool = False
    user: UserInfo = UserInfo()
    audiences: tuple[str, ...] = ()
    error: str | None = None


class TokenReviewResponse(_Response):
    status: TokenReviewStatus = TokenReviewStatus()
