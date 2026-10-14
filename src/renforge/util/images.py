"""Keep gameplay images small enough for common MCP event limits."""
from __future__ import annotations

import io
import struct

MCP_PNG_MAX_BYTES = 384 * 1024


def bounded_png(png: bytes) -> tuple[bytes, dict]:
    metadata = {}
    if len(png) >= 24 and png[:8] == b"\x89PNG\r\n\x1a\n":
        width, height = struct.unpack(">II", png[16:24])
        metadata = {"width": width, "height": height,
                    "original_width": width, "original_height": height,
                    "scaled": False}
    if len(png) <= MCP_PNG_MAX_BYTES:
        return png, metadata
    from PIL import Image

    with Image.open(io.BytesIO(png)) as source:
        image = source.convert("RGB")
        original_width, original_height = image.size
    image.thumbnail((1280, 1280), Image.Resampling.LANCZOS)
    while True:
        output = io.BytesIO()
        # optimize=True tries every filter at zlib level 9: about 8x slower
        # for ~6% less, and observations wait on it (2.7 s per 1080p frame).
        image.save(output, format="PNG", compress_level=6)
        data = output.getvalue()
        if len(data) <= MCP_PNG_MAX_BYTES:
            return data, {"width": image.width, "height": image.height,
                          "original_width": original_width, "original_height": original_height,
                          "scaled": image.size != (original_width, original_height)}
        # PNG size grows with the pixel count: aim just under the limit.
        factor = min(0.9, max(0.5, (MCP_PNG_MAX_BYTES / len(data)) ** 0.5 * 0.95))
        image = image.resize((max(1, int(image.width * factor)), max(1, int(image.height * factor))),
                             Image.Resampling.LANCZOS)
