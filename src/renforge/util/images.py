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
        image.save(output, format="PNG", optimize=True)
        data = output.getvalue()
        if len(data) <= MCP_PNG_MAX_BYTES:
            return data, {"width": image.width, "height": image.height,
                          "original_width": original_width, "original_height": original_height,
                          "scaled": image.size != (original_width, original_height)}
        image = image.resize((max(1, image.width * 3 // 4), max(1, image.height * 3 // 4)),
                             Image.Resampling.LANCZOS)
