"""Fit a judge request's images to the model API's documented limits by lossless tiling.

The credential proxy (tests/proxy/llm_proxy.py) passes every judge-listener request body through
``normalize`` before forwarding it. A request already within the limits below is returned as the
same bytes object, unchanged. Otherwise every image the limits reject is decoded and split into
tiles that each fit; every tile is encoded as PNG, which is lossless, so the tiles together hold
exactly the decoded pixels of the image they came from. A text block before each tile names the
source image (its position among the request's image blocks and the sha256 of its encoded bytes)
and the tile's pixel bounds. No image is cropped, dropped, downscaled, or reordered, and every
other block keeps its place. A request that cannot be brought within the limits this way raises
``Unrepresentable``, which the proxy answers as an error, never as a forwarded request.

Limits, from Anthropic's documentation as read on 2026-10-30:
https://platform.claude.com/docs/en/build-with-claude/vision ("Image limits and costs", "Request
limits", "Supported formats") and https://platform.claude.com/docs/en/api/overview ("Request size
limits"):

* each image at most 8000x8000 px;
* when one request carries more than 20 images (every image block counts, including those nested
  in tool_result content), every image in it at most 3,000 px on either side;
* each image at most 10 MB base64-encoded on the Claude API;
* at most 100 images per request for models with a 200k-token context window and 600 for all
  others; the smaller bound is applied, since this module does not know the judge model's window;
* a Messages or Token Counting request at most 128 MB;
* JPEG, PNG, GIF, and WebP only; animations are unsupported and only the first frame is used.

Byte quantities are read as decimal megabytes, the stricter reading. An image given by URL or by
Files API id cannot be measured here; it is counted and left as it is.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import io
import json

from PIL import Image

MAX_EDGE = 8000
MANY_IMAGE_THRESHOLD = 20
MANY_IMAGE_MAX_EDGE = 3000
MAX_IMAGE_BASE64_BYTES = 10_000_000
MAX_IMAGES_PER_REQUEST = 100
MAX_REQUEST_BYTES = 128_000_000
DOC_SOURCES = (
    "https://platform.claude.com/docs/en/build-with-claude/vision",
    "https://platform.claude.com/docs/en/api/overview",
)
# PNG stores these modes losslessly; any other decoded mode is refused rather than converted.
LOSSLESS_PNG_MODES = frozenset({"1", "L", "LA", "P", "RGB", "RGBA", "I;16"})
EXIF_ORIENTATION = 0x0112


class Unrepresentable(Exception):
    """The request cannot be brought within the documented limits without losing content."""


class _Image:
    """One image block found in the request, with where it sits."""

    def __init__(self, ordinal: int, container: list, block: dict) -> None:
        self.ordinal = ordinal
        self.container = container
        self.block = block
        source = block.get("source") if isinstance(block.get("source"), dict) else {}
        self.inline = source.get("type") == "base64" and isinstance(
            source.get("data"), str
        )
        self.data = source.get("data") if self.inline else ""
        self.size: tuple[int, int] | None = None
        self.raw = b""
        if self.inline:
            try:
                self.raw = base64.b64decode(self.data, validate=True)
                with Image.open(io.BytesIO(self.raw)) as probe:
                    self.size = probe.size
            except (binascii.Error, ValueError, OSError, Image.DecompressionBombError) as exc:
                raise Unrepresentable(
                    f"image {ordinal} cannot be decoded ({type(exc).__name__})"
                ) from exc

    def fits(self, edge: int) -> bool:
        if not self.inline or self.size is None:
            return True
        return max(self.size) <= edge and len(self.data) <= MAX_IMAGE_BASE64_BYTES


def _image_blocks(blocks: list, found: list) -> None:
    for block in blocks:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "image":
            found.append((blocks, block))
        elif isinstance(block.get("content"), list):
            _image_blocks(block["content"], found)


def _collect(document: dict) -> list[_Image]:
    found: list = []
    for message in document.get("messages", []):
        if isinstance(message, dict) and isinstance(message.get("content"), list):
            _image_blocks(message["content"], found)
    return [
        _Image(n, container, block)
        for n, (container, block) in enumerate(found, start=1)
    ]


def _grid(width: int, height: int, edge: int) -> list[tuple[int, int, int, int]]:
    return [
        (x, y, min(x + edge, width), min(y + edge, height))
        for y in range(0, height, edge)
        for x in range(0, width, edge)
    ]


def _tile_count(image: _Image, edge: int) -> int:
    if image.fits(edge):
        return 1
    width, height = image.size  # type: ignore[misc]
    return len(_grid(width, height, edge))


def _encode(tile: Image.Image, extra: dict) -> bytes:
    out = io.BytesIO()
    tile.save(out, format="PNG", optimize=False, **extra)
    return out.getvalue()


def _split(decoded: Image.Image, box: tuple[int, int, int, int], extra: dict) -> list:
    """Tiles of ``box`` whose base64 fits the per-image byte limit, halving the long side as needed."""
    encoded = _encode(decoded.crop(box), extra)
    if len(base64.b64encode(encoded)) <= MAX_IMAGE_BASE64_BYTES:
        return [(box, encoded)]
    x0, y0, x1, y1 = box
    if x1 - x0 <= 1 and y1 - y0 <= 1:
        raise Unrepresentable("a single pixel exceeds the per-image byte limit")
    if x1 - x0 >= y1 - y0:
        middle = x0 + (x1 - x0) // 2
        halves = [(x0, y0, middle, y1), (middle, y0, x1, y1)]
    else:
        middle = y0 + (y1 - y0) // 2
        halves = [(x0, y0, x1, middle), (x0, middle, x1, y1)]
    return [part for half in halves for part in _split(decoded, half, extra)]


def _tiles(image: _Image, edge: int) -> list[tuple[tuple[int, int, int, int], bytes]]:
    decoded = Image.open(io.BytesIO(image.raw))
    if getattr(decoded, "n_frames", 1) != 1:
        raise Unrepresentable(
            f"image {image.ordinal} is animated and too large to send whole"
        )
    orientation = decoded.getexif().get(EXIF_ORIENTATION, 1)
    if orientation != 1:
        raise Unrepresentable(
            f"image {image.ordinal} carries EXIF orientation {orientation}"
        )
    decoded.load()
    if decoded.mode not in LOSSLESS_PNG_MODES:
        raise Unrepresentable(
            f"image {image.ordinal} decodes to mode {decoded.mode}, which PNG cannot hold losslessly"
        )
    extra = {}
    if "transparency" in decoded.info:
        extra["transparency"] = decoded.info["transparency"]
    width, height = decoded.size
    tiles = [
        part
        for box in _grid(width, height, edge)
        for part in _split(decoded, box, extra)
    ]
    _verify(decoded, tiles, image.ordinal)
    return tiles


def _verify(decoded: Image.Image, tiles: list, ordinal: int) -> None:
    """Refuse unless the decoded tiles, pasted at their bounds, equal the decoded original exactly."""
    rebuilt = Image.new(decoded.mode, decoded.size)
    if decoded.mode == "P":
        rebuilt.putpalette(decoded.getpalette())
    covered = 0
    for (x0, y0, x1, y1), encoded in tiles:
        with Image.open(io.BytesIO(encoded)) as tile:
            tile.load()
            if tile.mode != decoded.mode or tile.size != (x1 - x0, y1 - y0):
                raise Unrepresentable(
                    f"image {ordinal}: a tile did not round-trip its mode or size"
                )
            if decoded.mode == "P" and tile.getpalette() != decoded.getpalette():
                raise Unrepresentable(
                    f"image {ordinal}: a tile did not round-trip its palette"
                )
            rebuilt.paste(tile, (x0, y0))
        covered += (x1 - x0) * (y1 - y0)
    if (
        covered != decoded.size[0] * decoded.size[1]
        or rebuilt.tobytes() != decoded.tobytes()
    ):
        raise Unrepresentable(
            f"image {ordinal}: the tiles do not reproduce the decoded pixels"
        )


def _label(image: _Image, total: int, index: int, count: int, box: tuple) -> dict:
    width, height = image.size  # type: ignore[misc]
    x0, y0, x1, y1 = box
    digest = hashlib.sha256(image.raw).hexdigest()
    return {
        "type": "text",
        "text": (
            f"[Transport note, not part of the evidence: image {image.ordinal} of {total} in this "
            f"request ({width}x{height} px, sha256 {digest}) exceeds the model API's per-image "
            f"limit, so it is sent as {count} lossless tiles in order, left to right within each "
            f"row, rows top to bottom. Tile {index} of {count} covers x {x0} to {x1} and "
            f"y {y0} to {y1} (end-exclusive) of that image.]"
        ),
    }


def _replace(image: _Image, total: int, tiles: list) -> None:
    keep = {
        k: v
        for k, v in image.block.items()
        if k not in ("type", "source", "cache_control")
    }
    blocks: list = []
    for index, (box, encoded) in enumerate(tiles, start=1):
        tile_block = {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": "image/png",
                "data": base64.b64encode(encoded).decode("ascii"),
            },
            **keep,
        }
        if index == len(tiles) and "cache_control" in image.block:
            tile_block["cache_control"] = image.block["cache_control"]
        blocks += [_label(image, total, index, len(tiles), box), tile_block]
    position = next(
        i for i, block in enumerate(image.container) if block is image.block
    )
    image.container[position : position + 1] = blocks


def normalize(body: bytes) -> tuple[bytes, dict | None]:
    """Return ``(body, None)`` when ``body`` already fits, else the tiled body and a provenance record.

    Raises ``Unrepresentable`` when no lossless tiling brings the request within the limits.
    """
    try:
        document = json.loads(body)
    except (UnicodeDecodeError, ValueError):
        return body, None
    if not isinstance(document, dict) or not isinstance(document.get("messages"), list):
        return body, None
    images = _collect(document)
    count = len(images)
    edge = MANY_IMAGE_MAX_EDGE if count > MANY_IMAGE_THRESHOLD else MAX_EDGE
    if (
        count <= MAX_IMAGES_PER_REQUEST
        and len(body) <= MAX_REQUEST_BYTES
        and all(image.fits(edge) for image in images)
    ):
        return body, None
    if (
        edge == MAX_EDGE
        and sum(_tile_count(i, edge) for i in images) > MANY_IMAGE_THRESHOLD
    ):
        edge = MANY_IMAGE_MAX_EDGE
    planned = [image for image in images if not image.fits(edge)]
    record: dict = {
        "images_in": count,
        "edge": edge,
        "tiled": [],
        "docs": list(DOC_SOURCES),
    }
    for image in reversed(planned):
        try:
            tiles = _tiles(image, edge)
        except (OSError, ValueError, Image.DecompressionBombError) as exc:
            raise Unrepresentable(
                f"image {image.ordinal} cannot be decoded for tiling ({type(exc).__name__})"
            ) from exc
        _replace(image, count, tiles)
        record["tiled"].insert(
            0,
            {
                "image": image.ordinal,
                "size": list(image.size),  # type: ignore[arg-type]
                "sha256": hashlib.sha256(image.raw).hexdigest(),
                "tiles": [list(box) for box, _ in tiles],
                "pixels_exact": True,
            },
        )
    after = _collect(document)
    record["images_out"] = len(after)
    if len(after) > MAX_IMAGES_PER_REQUEST:
        raise Unrepresentable(
            f"{len(after)} images after tiling, above the {MAX_IMAGES_PER_REQUEST}-image request limit"
        )
    if len(after) > MANY_IMAGE_THRESHOLD and not all(
        i.fits(MANY_IMAGE_MAX_EDGE) for i in after
    ):
        raise Unrepresentable(
            "an image still exceeds the many-image limit after tiling"
        )
    out = json.dumps(document, separators=(",", ":")).encode("ascii")
    if len(out) > MAX_REQUEST_BYTES:
        raise Unrepresentable(
            f"the request is {len(out)} bytes after tiling, above the {MAX_REQUEST_BYTES}-byte limit"
        )
    record["request_bytes_out"] = len(out)
    return out, record
