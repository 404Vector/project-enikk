from __future__ import annotations

import base64
import uuid
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import httpx
import pydantic_core
import pytest
from pydantic import SecretStr
from starlette.testclient import TestClient

from enikk_imagegen.app import HEALTH_PATH, MCP_PATH, create_app
from enikk_imagegen.files import ImageFileError, Workspace
from enikk_imagegen.gemini import (
    KEY_HEADER,
    GeminiError,
    GeminiImages,
    GeneratedImage,
    InlineImage,
    parse_response,
    request_body,
)
from enikk_imagegen.service import ImageService, LimitReachedError, Limits
from enikk_imagegen.tools import SESSION_HEADER
from enikk_kernel.errors import EnikkError
from enikk_langfuse.styles import Style, StyleError

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence
    from pathlib import Path

    from enikk_langfuse.images import ImageRecord

pytestmark = pytest.mark.anyio

PNG = b"\x89PNG\r\n\x1a\nfake"
SESSION = uuid.uuid4()
HOST = "testserver"
MCP_HEADERS = {"accept": "application/json, text/event-stream", SESSION_HEADER: str(SESSION)}


def _image_response(*, thought: bool = False) -> bytes:
    parts: list[dict[str, object]] = []
    if thought:
        parts.append({"inlineData": {"mimeType": "image/png", "data": "AAAA"}, "thought": True})
    parts += [
        {"text": "Here it is."},
        {"inlineData": {"mimeType": "image/png", "data": base64.b64encode(PNG).decode()}},
    ]
    return pydantic_core.to_json(
        {
            "candidates": [{"content": {"parts": parts}, "finishReason": "STOP"}],
            "usageMetadata": {"promptTokenCount": 12, "candidatesTokenCount": 1290},
        }
    )


class FakeModel:
    def __init__(self, error: GeminiError | None = None) -> None:
        self.calls: list[tuple[str, str, int, str | None, str | None]] = []
        self._error = error

    async def generate(
        self,
        model: str,
        prompt: str,
        images: Sequence[InlineImage],
        *,
        aspect_ratio: str | None,
        image_size: str | None,
    ) -> GeneratedImage:
        self.calls.append((model, prompt, len(images), aspect_ratio, image_size))
        if self._error is not None:
            raise self._error
        return GeneratedImage(
            image=InlineImage(mime_type="image/png", data=PNG),
            text="ok",
            input_tokens=10,
            output_tokens=1290,
        )


class Recorder:
    def __init__(self) -> None:
        self.records: list[ImageRecord] = []

    def record(self, record: ImageRecord) -> None:
        self.records.append(record)


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 11, 25, 14, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now


def _limits(daily_limit: int = 100) -> Limits:
    return Limits(
        daily_limit=daily_limit,
        timezone="Asia/Seoul",
        default_model="gemini-3.1-flash-image",
        models=("gemini-3.1-flash-image", "gemini-3-pro-image"),
        prices={"gemini-3.1-flash-image": {"1K": 0.067}},
    )


def _service(
    tmp_path: Path,
    model: FakeModel,
    recorder: Recorder,
    *,
    daily_limit: int = 100,
    clock: Clock | None = None,
    styles: FakeStyles | None = None,
) -> ImageService:
    return ImageService(
        model,
        Workspace(tmp_path),
        _limits(daily_limit),
        recorder=recorder,
        now=clock or Clock(),
        styles=styles,
    )


PIXEL = Style(
    name="style-pixel",
    version=3,
    text="16-bit pixel art.",
    model="gemini-3-pro-image",
    description="sprites",
)


class FakeStyles:
    def __init__(self, *styles: Style) -> None:
        self._styles = {style.name: style for style in styles}

    async def get(self, name: str) -> Style:
        if name not in self._styles:
            raise StyleError(f"style '{name}' has no production version in Langfuse")
        return self._styles[name]

    async def available(self) -> tuple[Style, ...]:
        return tuple(self._styles.values())


def test_request_body_puts_images_before_the_text() -> None:
    body = request_body("a knight", [InlineImage(mime_type="image/png", data=PNG)], "16:9", "2K")
    text = body.decode()
    assert '"inline_data":{"mime_type":"image/png"' in text
    assert text.index("inline_data") < text.index('"text":"a knight"')
    assert '"responseModalities":["TEXT","IMAGE"]' in text
    assert '"imageConfig":{"aspectRatio":"16:9","imageSize":"2K"}' in text


def test_request_body_leaves_out_unset_options() -> None:
    assert '"imageConfig":{}' in request_body("x", [], None, None).decode()


def test_response_gives_the_last_image_that_is_not_a_thought() -> None:
    made = parse_response(_image_response(thought=True))
    assert made.image.data == PNG
    assert made.text == "Here it is."
    assert (made.input_tokens, made.output_tokens) == (12, 1290)


def test_response_without_an_image_is_an_error() -> None:
    raw = pydantic_core.to_json(
        {"candidates": [{"content": {"parts": [{"text": "I can't."}]}, "finishReason": "STOP"}]}
    )
    with pytest.raises(GeminiError, match="no image: I can't"):
        parse_response(raw)


def test_blocked_prompt_is_an_error() -> None:
    raw = pydantic_core.to_json({"promptFeedback": {"blockReason": "SAFETY"}})
    with pytest.raises(GeminiError, match="blocked: SAFETY"):
        parse_response(raw)


async def test_client_sends_the_key_in_a_header_only() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, content=_image_response())

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = GeminiImages(http, "https://gemini.test/v1/", SecretStr("k-123"))
        made = await client.generate("m-1", "p", [], aspect_ratio=None, image_size="1K")
    assert made.image.data == PNG
    request = seen[0]
    assert str(request.url) == "https://gemini.test/v1/models/m-1:generateContent"
    assert request.headers[KEY_HEADER] == "k-123"
    assert "k-123" not in str(request.url)
    assert b"k-123" not in request.content


async def test_client_error_carries_status_but_not_the_key() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        body = {"error": {"status": "INVALID_ARGUMENT", "message": "API key not valid."}}
        return httpx.Response(400, content=pydantic_core.to_json(body))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = GeminiImages(http, "https://gemini.test/v1", SecretStr("k-123"))
        with pytest.raises(GeminiError) as caught:
            await client.generate("m", "p", [], aspect_ratio=None, image_size=None)
    assert "400 INVALID_ARGUMENT: API key not valid." in str(caught.value)
    assert "k-123" not in str(caught.value)


def test_workspace_reads_only_the_threads_own_files(tmp_path: Path) -> None:
    workspace = Workspace(tmp_path)
    own = tmp_path / "tmp" / str(SESSION) / "ref.png"
    own.parent.mkdir(parents=True)
    own.write_bytes(PNG)
    other = tmp_path / "tmp" / str(uuid.uuid4()) / "ref.png"
    other.parent.mkdir(parents=True)
    other.write_bytes(PNG)
    assert workspace.read(SESSION, str(own)).mime_type == "image/png"
    with pytest.raises(ImageFileError, match="only images under this thread"):
        workspace.read(SESSION, str(other))
    with pytest.raises(ImageFileError, match="only images under this thread"):
        workspace.read(SESSION, str(own.parent / ".." / str(other.parent.name) / "ref.png"))
    (own.parent / "notes.txt").write_text("x")
    with pytest.raises(ImageFileError, match=r"\.png"):
        workspace.read(SESSION, str(own.parent / "notes.txt"))


def test_workspace_saves_under_the_threads_images(tmp_path: Path) -> None:
    workspace = Workspace(tmp_path)
    now = datetime(2026, 11, 25, 1, 2, 3, tzinfo=UTC)
    image = InlineImage(mime_type="image/png", data=PNG)
    first = workspace.save(SESSION, image, name="Hero Sprite!", now=now)
    second = workspace.save(SESSION, image, name="Hero Sprite!", now=now)
    assert first == tmp_path / "tmp" / str(SESSION) / "images" / "20261125-010203-hero-sprite.png"
    assert second.name == "20261125-010203-hero-sprite-2.png"
    assert first.read_bytes() == PNG


async def test_service_saves_counts_and_traces(tmp_path: Path) -> None:
    model, recorder = FakeModel(), Recorder()
    service = _service(tmp_path, model, recorder)
    result = await service.make(
        tool="generate_image",
        session=SESSION,
        prompt="a knight",
        inputs=[],
        name="knight",
        model=None,
        aspect_ratio="1:1",
        image_size="1K",
    )
    assert model.calls == [("gemini-3.1-flash-image", "a knight", 0, "1:1", "1K")]
    assert result.used_today == 1
    assert result.estimated_cost_usd == 0.067
    (record,) = recorder.records
    assert record.session_id == SESSION
    assert record.output == result.path
    assert record.output_tokens == 1290
    assert record.cost_usd == 0.067
    assert record.error is None


async def test_service_refuses_other_models(tmp_path: Path) -> None:
    service = _service(tmp_path, FakeModel(), Recorder())
    assert service.model_for("gemini-3-pro-image") == "gemini-3-pro-image"
    with pytest.raises(Exception, match="not allowed"):
        service.model_for("imagen-x")


async def test_daily_limit_resets_at_midnight_in_the_timezone(tmp_path: Path) -> None:
    clock = Clock()
    service = _service(tmp_path, FakeModel(), Recorder(), daily_limit=1, clock=clock)
    arguments = {
        "tool": "generate_image",
        "session": SESSION,
        "prompt": "p",
        "inputs": [],
        "name": "n",
        "model": None,
        "aspect_ratio": None,
        "image_size": "1K",
    }
    await service.make(**arguments)  # type: ignore[arg-type]
    with pytest.raises(LimitReachedError, match="limit of 1"):
        await service.make(**arguments)  # type: ignore[arg-type]
    clock.now += timedelta(hours=1)  # 00:00 in Seoul
    assert (await service.make(**arguments)).used_today == 1  # type: ignore[arg-type]


async def test_failed_call_gives_its_count_back_and_is_traced(tmp_path: Path) -> None:
    recorder = Recorder()
    service = _service(tmp_path, FakeModel(GeminiError("Gemini API 429")), recorder, daily_limit=1)
    with pytest.raises(GeminiError):
        await service.make(
            tool="generate_image",
            session=SESSION,
            prompt="p",
            inputs=[],
            name="n",
            model=None,
            aspect_ratio=None,
            image_size="1K",
        )
    (record,) = recorder.records
    assert record.error == "Gemini API 429"
    assert record.output is None
    assert await service._count.take() == 1


@pytest.fixture
def mcp(tmp_path: Path) -> Iterator[tuple[TestClient, FakeModel]]:
    model = FakeModel()
    app = create_app(_service(tmp_path, model, Recorder()), allowed_hosts=(HOST,))
    with TestClient(app) as client:
        yield client, model


def _call(
    client: TestClient, name: str, arguments: dict[str, object], headers: dict[str, str]
) -> dict[str, object]:
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": name, "arguments": arguments},
    }
    response = client.post(MCP_PATH, json=body, headers=headers)
    assert response.status_code == 200
    result: dict[str, object] = response.json()["result"]
    return result


def test_health(mcp: tuple[TestClient, FakeModel]) -> None:
    client, _ = mcp
    assert client.get(HEALTH_PATH).json() == {"status": "ok"}


def test_tools_are_listed(mcp: tuple[TestClient, FakeModel]) -> None:
    client, _ = mcp
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
    tools = client.post(MCP_PATH, json=body, headers=MCP_HEADERS).json()["result"]["tools"]
    assert {tool["name"] for tool in tools} == {
        "generate_image",
        "edit_image",
        "list_styles",
        "add_style_reference",
        "remove_style_reference",
    }


def test_generate_image_over_mcp(mcp: tuple[TestClient, FakeModel], tmp_path: Path) -> None:
    client, model = mcp
    result = _call(client, "generate_image", {"prompt": "a slime", "name": "slime"}, MCP_HEADERS)
    assert result["isError"] is False
    path = str(result["structuredContent"]["path"])  # type: ignore[index]
    assert path.startswith(str(tmp_path / "tmp" / str(SESSION) / "images"))
    assert model.calls == [("gemini-3.1-flash-image", "a slime", 0, None, "1K")]


def test_edit_image_sends_the_image_first(
    mcp: tuple[TestClient, FakeModel], tmp_path: Path
) -> None:
    client, model = mcp
    source = tmp_path / "tmp" / str(SESSION) / "images" / "old.png"
    source.parent.mkdir(parents=True)
    source.write_bytes(PNG)
    arguments: dict[str, object] = {"image": str(source), "instruction": "make it blue"}
    result = _call(client, "edit_image", arguments, MCP_HEADERS)
    assert result["isError"] is False
    assert model.calls == [("gemini-3.1-flash-image", "make it blue", 1, None, "1K")]


def test_a_call_without_a_session_is_refused(mcp: tuple[TestClient, FakeModel]) -> None:
    client, model = mcp
    headers = {"accept": MCP_HEADERS["accept"]}
    result = _call(client, "generate_image", {"prompt": "p"}, headers)
    assert result["isError"] is True
    assert model.calls == []


def _own_image(tmp_path: Path, name: str = "ref.png") -> Path:
    path = tmp_path / "tmp" / str(SESSION) / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(PNG)
    return path


async def test_a_style_goes_before_the_prompt_with_its_references(tmp_path: Path) -> None:
    model, recorder = FakeModel(), Recorder()
    service = _service(tmp_path, model, recorder, styles=FakeStyles(PIXEL))
    await service.add_reference(SESSION, "style-pixel", str(_own_image(tmp_path)))
    result = await service.make(
        tool="generate_image",
        session=SESSION,
        prompt="a slime",
        inputs=[],
        name="slime",
        model=None,
        aspect_ratio=None,
        image_size="1K",
        style="style-pixel",
    )
    assert model.calls == [("gemini-3-pro-image", "16-bit pixel art.\n\na slime", 1, None, "1K")]
    assert (result.style, result.style_version) == ("style-pixel", 3)
    (record,) = recorder.records
    assert (record.style, record.style_version) == ("style-pixel", 3)


async def test_the_requested_model_wins_over_the_styles(tmp_path: Path) -> None:
    model = FakeModel()
    service = _service(tmp_path, model, Recorder(), styles=FakeStyles(PIXEL))
    await service.make(
        tool="generate_image",
        session=SESSION,
        prompt="p",
        inputs=[],
        name="n",
        model="gemini-3.1-flash-image",
        aspect_ratio=None,
        image_size="1K",
        style="style-pixel",
    )
    assert model.calls[0][0] == "gemini-3.1-flash-image"


async def test_an_unknown_style_is_refused_before_any_call(tmp_path: Path) -> None:
    model = FakeModel()
    service = _service(tmp_path, model, Recorder(), styles=FakeStyles(PIXEL))
    with pytest.raises(EnikkError, match="no production version"):
        await service.make(
            tool="generate_image",
            session=SESSION,
            prompt="p",
            inputs=[],
            name="n",
            model=None,
            aspect_ratio=None,
            image_size="1K",
            style="style-none",
        )
    assert model.calls == []
    assert await service._count.take() == 1


async def test_style_references_are_listed_added_and_removed(tmp_path: Path) -> None:
    service = _service(tmp_path, FakeModel(), Recorder(), styles=FakeStyles(PIXEL))
    summary = await service.add_reference(SESSION, "style-pixel", str(_own_image(tmp_path)))
    assert summary.references == ["ref.png"]
    assert (tmp_path / "styles" / "style-pixel" / "ref.png").read_bytes() == PNG
    (listed,) = await service.styles()
    assert (listed.name, listed.version, listed.references) == ("style-pixel", 3, ["ref.png"])
    assert (await service.remove_reference("style-pixel", "ref.png")).references == []
    with pytest.raises(ImageFileError, match="no reference image"):
        await service.remove_reference("style-pixel", "../../tmp/x.png")


async def test_a_style_holds_at_most_six_references(tmp_path: Path) -> None:
    service = _service(tmp_path, FakeModel(), Recorder(), styles=FakeStyles(PIXEL))
    for index in range(6):
        await service.add_reference(
            SESSION, "style-pixel", str(_own_image(tmp_path, f"r{index}.png"))
        )
    with pytest.raises(ImageFileError, match="already has 6"):
        await service.add_reference(SESSION, "style-pixel", str(_own_image(tmp_path, "r7.png")))


async def test_references_of_another_thread_cannot_become_style_references(tmp_path: Path) -> None:
    service = _service(tmp_path, FakeModel(), Recorder(), styles=FakeStyles(PIXEL))
    other = tmp_path / "tmp" / str(uuid.uuid4()) / "x.png"
    other.parent.mkdir(parents=True)
    other.write_bytes(PNG)
    with pytest.raises(ImageFileError, match="only images under this thread"):
        await service.add_reference(SESSION, "style-pixel", str(other))


async def test_without_langfuse_styles_are_off(tmp_path: Path) -> None:
    service = _service(tmp_path, FakeModel(), Recorder())
    assert await service.styles() == []
    with pytest.raises(EnikkError, match="styles are off"):
        await service.add_reference(SESSION, "style-pixel", str(_own_image(tmp_path)))
