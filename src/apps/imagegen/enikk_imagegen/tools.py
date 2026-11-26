"""The image MCP tools (ADR-0043): make an image, or change one, from text, optional
reference images and an optional style. Each saves a file in the calling thread's tmp.
And the styles: list them, add or remove their reference images."""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Annotated, Any, Final, Literal

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp_types import ToolAnnotations
from pydantic import Field

from enikk_kernel.errors import EnikkError

if TYPE_CHECKING:
    from collections.abc import Awaitable

    from enikk_imagegen.service import ImageResult, ImageService, StyleSummary

SERVER_NAME: Final = "enikk-imagegen"
SESSION_HEADER: Final = "x-enikk-session-id"
MAKES: Final = ToolAnnotations(
    read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=True
)
READS: Final = ToolAnnotations(read_only_hint=True, open_world_hint=False)
KEEPS: Final = ToolAnnotations(
    read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=False
)
REMOVES: Final = ToolAnnotations(
    read_only_hint=False, destructive_hint=True, idempotent_hint=True, open_world_hint=False
)

AspectRatio = Literal[
    "1:1", "1:4", "1:8", "2:3", "3:2", "3:4", "4:1", "4:3", "4:5", "5:4", "8:1", "9:16",
    "16:9", "21:9",
]  # fmt: skip
ImageSize = Literal["512", "1K", "2K", "4K"]
Prompt = Annotated[str, Field(min_length=1, max_length=8000)]
References = Annotated[list[str], Field(max_length=14)]
Name = Annotated[str, Field(max_length=60)]
StyleName = Annotated[str, Field(pattern=r"^style-[a-z0-9][a-z0-9-]{0,62}$")]


def build_server(service: ImageService) -> MCPServer:
    server = MCPServer(
        SERVER_NAME,
        instructions=(
            "Image generation for game assets with the Gemini API (ADR-0043). Each call "
            "costs money and counts toward a daily budget in estimated USD (ADR-0045). "
            "Images are saved under /data/tmp/<this thread>/images. A style (list_styles) "
            "puts its description and reference images before the request."
        ),
    )

    @server.tool(annotations=MAKES)
    async def generate_image(
        prompt: Prompt,
        ctx: Context,
        name: Name = "",
        aspect_ratio: AspectRatio | None = None,
        image_size: ImageSize = "1K",
        reference_images: References | None = None,
        model: str | None = None,
        style: StyleName | None = None,
    ) -> dict[str, Any]:
        """Make a new image from a description. name: a short file name hint.
        reference_images: paths of PNG/JPEG/WebP files under this thread's tmp or
        worktree, used for characters or objects (with the style's, up to 14 in all).
        style: a style from list_styles; its description goes before the prompt and its
        reference images are sent too. model: leave empty for the style's or the
        default; ask for gemini-3-pro-image only when the owner wants it.
        Returns the saved path and an estimated cost."""
        return await _call(
            service.make(
                tool="generate_image",
                session=_session(ctx),
                prompt=prompt,
                inputs=reference_images or [],
                name=name or "image",
                model=model,
                aspect_ratio=aspect_ratio,
                image_size=image_size,
                style=style,
            )
        )

    @server.tool(annotations=MAKES)
    async def edit_image(
        image: str,
        instruction: Prompt,
        ctx: Context,
        name: Name = "",
        aspect_ratio: AspectRatio | None = None,
        image_size: ImageSize = "1K",
        reference_images: References | None = None,
        model: str | None = None,
        style: StyleName | None = None,
    ) -> dict[str, Any]:
        """Change an existing image as instructed and save the result as a new file.
        image: path of the image to change (this thread's tmp or worktree).
        reference_images, model and style as in generate_image."""
        return await _call(
            service.make(
                tool="edit_image",
                session=_session(ctx),
                prompt=instruction,
                inputs=[image, *(reference_images or [])],
                name=name or "edit",
                model=model,
                aspect_ratio=aspect_ratio,
                image_size=image_size,
                style=style,
            )
        )

    @server.tool(annotations=READS)
    async def list_styles() -> dict[str, Any]:
        """The image styles (Langfuse prompts style-*, production versions) with what
        each is for, its model and its reference image file names. Styles are created
        and changed as prompt candidates the owner promotes."""
        try:
            styles = await service.styles()
        except EnikkError as exc:
            raise ToolError(str(exc)) from exc
        return {"styles": [style.model_dump(mode="json") for style in styles]}

    @server.tool(annotations=KEEPS)
    async def add_style_reference(style: StyleName, image: str, ctx: Context) -> dict[str, Any]:
        """Keep one of this thread's images (tmp or worktree path) as a reference image
        of a style; it is sent with every later request in that style (up to 6)."""
        return await _summary(service.add_reference(_session(ctx), style, image))

    @server.tool(annotations=REMOVES)
    async def remove_style_reference(style: StyleName, file_name: str) -> dict[str, Any]:
        """Stop using one reference image of a style. file_name: as list_styles shows."""
        return await _summary(service.remove_reference(style, file_name))

    return server


async def _summary(pending: Awaitable[StyleSummary]) -> dict[str, Any]:
    try:
        summary = await pending
    except EnikkError as exc:
        raise ToolError(str(exc)) from exc
    dumped: dict[str, Any] = summary.model_dump(mode="json")
    return dumped


def _session(ctx: Context) -> uuid.UUID:
    """The calling session, set by the gateway from core's token (ADR-0041 §3)."""
    headers = {key.lower(): value for key, value in (ctx.headers or {}).items()}
    try:
        return uuid.UUID(headers[SESSION_HEADER])
    except (KeyError, ValueError) as exc:
        raise ToolError(f"the request must carry {SESSION_HEADER} as a UUID") from exc


async def _call(pending: Awaitable[ImageResult]) -> dict[str, Any]:
    try:
        result = await pending
    except EnikkError as exc:
        raise ToolError(str(exc)) from exc
    dumped: dict[str, Any] = result.model_dump(mode="json")
    return dumped
