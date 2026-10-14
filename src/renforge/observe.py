"""Host screenshot decoding and compatibility exports for gameplay helpers."""
from __future__ import annotations

import base64
import hashlib
from collections.abc import Mapping
from typing import Any
from .util.images import bounded_png

# Re-export the existing public helpers for callers of renforge.observe.
from .interaction import (
    READOUT_LIMIT, READOUT_TEXT_LIMIT, assign_control_ids, classify,
    crossed_transcript, guard_act, guard_dismiss, image_basename,
    normalize_role, normalize_say_dismiss, operations_for, strip_text_tags,
    progression_boundary,
)

from .progression import POLL_SECONDS, run_advance_until

def apply_screenshot(raw: Mapping[str, Any]) -> tuple[dict[str, Any], bytes | None]:
    """Bound delivered images while preserving their source frame guard."""
    data = dict(raw)
    encoded = data.pop("screenshot_base64", None)
    source_hash = data.pop("sha256", None)
    if encoded is None:
        return data, None
    if not isinstance(encoded, str) or not encoded:
        raise ValueError("screenshot_base64 must be a non-empty string")
    png = base64.b64decode(encoded, validate=True)
    data["frame_id"] = source_hash or hashlib.sha256(png).hexdigest()
    png, metadata = bounded_png(png)
    data["screenshot"] = metadata
    data["frame_hash"] = hashlib.sha256(png).hexdigest()
    frame = dict(data.get("frame") or {})
    frame.update(metadata)
    frame.update(frame_id=data["frame_id"], session_id=data.get("session_id"))
    data["frame"] = frame
    return data, png
