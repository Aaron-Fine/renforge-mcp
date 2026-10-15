"""Compact observations for agents: the same facts without defaults or repeats.

The play tools always return this form; ``renforge_explain(id=...)`` gives a
control's full record. It keeps every fact an agent acts on and drops fields
that only restate defaults (``null``, ``false``, empty lists), per-occurrence
render paths, and data repeated elsewhere. Chrome (the game's menu and quick-menu buttons) is reduced to ids
and labels.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

# Shown only when they say something other than the usual "yes".
_TRUE_BY_DEFAULT = ("enabled", "clickable", "visible")
_DROPPED = {"occurrences", "layer", "screen", "synthetic", "semantic", "render_path"}


def _centre(bounds: Any) -> dict[str, int] | None:
    if not isinstance(bounds, Mapping):
        return None
    try:
        return {"x": int(bounds["x"] + bounds["width"] // 2), "y": int(bounds["y"] + bounds["height"] // 2)}
    except (KeyError, TypeError):
        return None


def compact_control(control: Mapping[str, Any], *, chrome: bool = False) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in control.items():
        if key in _DROPPED:
            continue
        if key in _TRUE_BY_DEFAULT:
            if value is not True:
                out[key] = value
            continue
        if value is None or value is False or value == [] or value == {}:
            continue
        if key == "click_point" and value == _centre(control.get("bounds")):
            continue
        out[key] = value
    if chrome:
        keep = {"id", "text", *_TRUE_BY_DEFAULT, "covered"}
        out = {key: value for key, value in out.items() if key in keep}
    return out


def compact_observation(snapshot: Any) -> Any:
    """Return the compact observation; never mutates the snapshot."""
    if not isinstance(snapshot, Mapping):
        return snapshot
    out: dict[str, Any] = {}
    for key, value in snapshot.items():
        if value is None:
            continue
        if key == "keys" and not value:
            continue
        if key == "controls" and isinstance(value, list):
            out[key] = [compact_control(item) for item in value if isinstance(item, Mapping)]
        elif key == "chrome" and isinstance(value, list):
            out[key] = [compact_control(item, chrome=True) for item in value if isinstance(item, Mapping)]
        elif key == "input_candidates" and isinstance(value, list):
            out[key] = [compact_control(item) for item in value if isinstance(item, Mapping)]
        elif key == "readout" and isinstance(value, list):
            out[key] = [{k: v for k, v in item.items() if k != "bounds"} if isinstance(item, Mapping) else item
                        for item in value]
        elif key == "omitted" and isinstance(value, Mapping):
            flagged = {k: v for k, v in value.items() if v}
            if flagged:
                out[key] = flagged
        elif key == "frame" and isinstance(value, Mapping):
            out[key] = {k: v for k, v in value.items() if k != "session_id"}
        elif key == "screenshot" and isinstance(snapshot.get("frame"), Mapping):
            continue  # the frame already gives the image size
        elif key == "frame_hash" and value == snapshot.get("frame_id"):
            continue
        elif key == "screens" and isinstance(value, list):
            out[key] = [{k: v for k, v in item.items() if v is not False} if isinstance(item, Mapping) else item
                        for item in value]
        else:
            out[key] = value
    return out
