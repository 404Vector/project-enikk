"""The Gemini API's ``generateContent`` for image models (ADR-0043).

Request: text and inline images in one user turn, ``responseModalities`` TEXT and
IMAGE, ``imageConfig`` with aspect ratio and size. Response: the last image part that
is not a thought, and the token counts.
"""

from __future__ import annotations

import base64
from typing import TYPE_CHECKING, Final

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from enikk_kernel.errors import EnikkError
from enikk_kernel.models import FrozenModel

if TYPE_CHECKING:
    from collections.abc import Sequence

    from pydantic import SecretStr

KEY_HEADER: Final = "x-goog-api-key"


class GeminiError(EnikkError):
    """The API refused or failed the request, or made no image. Never holds the key."""


class InlineImage(FrozenModel):
    mime_type: str
    data: bytes


class GeneratedImage(FrozenModel):
    image: InlineImage
    text: str
    input_tokens: int | None
    output_tokens: int | None


class _Wire(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="ignore")


class _InlineData(_Wire):
    mime_type: str = Field(alias="mimeType")
    data: str


class _Part(_Wire):
    text: str | None = None
    inline_data: _InlineData | None = Field(default=None, alias="inlineData")
    thought: bool = False


class _Content(_Wire):
    parts: list[_Part] = []


class _Candidate(_Wire):
    content: _Content | None = None
    finish_reason: str | None = Field(default=None, alias="finishReason")


class _Usage(_Wire):
    prompt_token_count: int | None = Field(default=None, alias="promptTokenCount")
    candidates_token_count: int | None = Field(default=None, alias="candidatesTokenCount")


class _PromptFeedback(_Wire):
    block_reason: str | None = Field(default=None, alias="blockReason")


class _Response(_Wire):
    candidates: list[_Candidate] = []
    usage_metadata: _Usage | None = Field(default=None, alias="usageMetadata")
    prompt_feedback: _PromptFeedback | None = Field(default=None, alias="promptFeedback")


class _ErrorBody(_Wire):
    message: str = ""
    status: str = ""


class _Error(_Wire):
    error: _ErrorBody


class _RequestInline(_Wire):
    mime_type: str
    data: str


class _RequestPart(_Wire):
    text: str | None = None
    inline_data: _RequestInline | None = None


class _RequestContent(_Wire):
    role: str = "user"
    parts: list[_RequestPart]


class _ImageConfig(_Wire):
    aspect_ratio: str | None = Field(default=None, serialization_alias="aspectRatio")
    image_size: str | None = Field(default=None, serialization_alias="imageSize")


class _GenerationConfig(_Wire):
    response_modalities: list[str] = Field(serialization_alias="responseModalities")
    image_config: _ImageConfig = Field(serialization_alias="imageConfig")


class _Request(_Wire):
    contents: list[_RequestContent]
    generation_config: _GenerationConfig = Field(serialization_alias="generationConfig")


def request_body(
    prompt: str, images: Sequence[InlineImage], aspect_ratio: str | None, image_size: str | None
) -> bytes:
    """The JSON body of one ``generateContent`` call: images first, then the text."""
    parts = [
        _RequestPart(
            inline_data=_RequestInline(
                mime_type=image.mime_type, data=base64.b64encode(image.data).decode()
            )
        )
        for image in images
    ]
    parts.append(_RequestPart(text=prompt))
    body = _Request(
        contents=[_RequestContent(parts=parts)],
        generation_config=_GenerationConfig(
            response_modalities=["TEXT", "IMAGE"],
            image_config=_ImageConfig(aspect_ratio=aspect_ratio, image_size=image_size),
        ),
    )
    return body.model_dump_json(by_alias=True, exclude_none=True).encode()


def parse_response(raw: bytes) -> GeneratedImage:
    try:
        response = _Response.model_validate_json(raw)
    except ValidationError as exc:
        raise GeminiError(
            f"unexpected response from the Gemini API: {exc.error_count()} errors"
        ) from exc
    if response.prompt_feedback and response.prompt_feedback.block_reason:
        raise GeminiError(f"the prompt was blocked: {response.prompt_feedback.block_reason}")
    image: InlineImage | None = None
    texts: list[str] = []
    finish: str | None = None
    for candidate in response.candidates[:1]:
        finish = candidate.finish_reason
        for part in candidate.content.parts if candidate.content else []:
            if part.thought:
                continue
            if part.inline_data is not None:
                image = InlineImage(
                    mime_type=part.inline_data.mime_type,
                    data=base64.b64decode(part.inline_data.data),
                )
            elif part.text:
                texts.append(part.text)
    text = "\n".join(texts).strip()
    if image is None:
        detail = text or f"finish reason {finish or 'unknown'}"
        raise GeminiError(f"the model made no image: {detail}")
    usage = response.usage_metadata
    return GeneratedImage(
        image=image,
        text=text,
        input_tokens=usage.prompt_token_count if usage else None,
        output_tokens=usage.candidates_token_count if usage else None,
    )


class GeminiImages:
    """Calls one image model per request with the key held in memory (I-09)."""

    def __init__(self, http: httpx.AsyncClient, base_url: str, key: SecretStr) -> None:
        self._http = http
        self._base_url = base_url.rstrip("/")
        self._key = key

    async def generate(
        self,
        model: str,
        prompt: str,
        images: Sequence[InlineImage],
        *,
        aspect_ratio: str | None,
        image_size: str | None,
    ) -> GeneratedImage:
        url = f"{self._base_url}/models/{model}:generateContent"
        try:
            response = await self._http.post(
                url,
                content=request_body(prompt, images, aspect_ratio, image_size),
                headers={
                    KEY_HEADER: self._key.get_secret_value(),
                    "content-type": "application/json",
                },
            )
        except httpx.HTTPError as exc:
            raise GeminiError(f"could not reach the Gemini API: {type(exc).__name__}") from exc
        if response.status_code != httpx.codes.OK:
            raise GeminiError(_error_message(response))
        return parse_response(response.content)


def _error_message(response: httpx.Response) -> str:
    try:
        body = _Error.model_validate_json(response.content).error
        detail = f"{body.status}: {body.message}".strip(": ")
    except ValidationError:
        detail = ""
    return f"Gemini API {response.status_code}" + (f" {detail}" if detail else "")
