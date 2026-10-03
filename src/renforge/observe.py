"""Classify one interaction snapshot into controls, chrome, and forward.

The injected bridge extracts records and mirrors :func:`assign_control_ids`,
:func:`guard_act`, and :func:`guard_dismiss`. It cannot import this module.
Keep those three predicates in sync with ``bridge.rpy``.
"""

from __future__ import annotations

import base64
import hashlib
import time
from collections.abc import Callable, Mapping
from typing import Any

READOUT_LIMIT = 64
READOUT_TEXT_LIMIT = 400

POLL_SECONDS = 0.05

_BUTTON_ROLES = {"button", "imagebutton", "textbutton", "hotspot", "imagemap"}
_BAR_ROLES = {"bar", "slider"}
_TEXT_ROLES = {"input"}
_EMPTY_ROLES = {"viewport", "drag", "draggroup"}
_STOPS = {"choose", "wait", "none", "max_steps", "timeout", "stalled", "crash"}


def normalize_role(role: Any) -> str:
    raw = role.strip().casefold() if isinstance(role, str) else ""
    if raw in _BUTTON_ROLES or "button" in raw or "hotspot" in raw:
        return "button"
    if raw in _BAR_ROLES:
        return "bar"
    if raw in _TEXT_ROLES:
        return "input"
    if raw in _EMPTY_ROLES or raw.startswith("drag"):
        return "drag" if raw.startswith("drag") else raw
    return raw or "unknown"


def operations_for(role: str) -> list[str]:
    if role == "button":
        return ["click"]
    if role == "input":
        return ["text"]
    if role == "bar" or role == "viewport":
        return ["value"]
    if role == "drag":
        return ["drop"]
    return []


def image_basename(image_name: Any) -> str | None:
    if isinstance(image_name, (list, tuple)):
        parts = [part.strip() for part in image_name if isinstance(part, str) and part.strip()]
        image_name = "/".join(parts)
    if not isinstance(image_name, str):
        return None
    name = image_name.strip().replace("\\", "/")
    if not name or name in {".", ".."}:
        return None
    base = name.rsplit("/", 1)[-1].strip()
    if not base or base in {".", ".."}:
        return None
    return base


def _screen_name(value: Any) -> str | None:
    if isinstance(value, (list, tuple)):
        value = value[0] if value else None
    if isinstance(value, str):
        text = value.strip()
        return text or None
    return None


def _menu_index(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _widget_id(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None


def _action_text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None


def assign_control_ids(elements: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Assign canonical ids. The bridge copies this order.

    Widget id, else menu item index, else ``drag_name``, else the action
    string, else the idle-image basename, else ``screen/role/ordinal`` with
    ``synthetic`` set. A drag name or action shared by siblings gains that
    image basename. Further duplicates gain ``#2``. ``image`` is always the
    basename, or null.
    """
    drafts: list[tuple[str, str, str | None, str | None, str, bool, Mapping[str, Any]]] = []
    split_counts: dict[str, int] = {}
    for fallback, element in enumerate(elements):
        screen = _screen_name(element.get("screen"))
        role = normalize_role(element.get("role"))
        widget_id = _widget_id(element.get("widget_id"))
        menu_index = _menu_index(element.get("menu_index"))
        drag_name = _widget_id(element.get("drag_name"))
        image = image_basename(element.get("image_name"))
        action = _action_text(element.get("action"))
        synthetic = False
        if widget_id:
            base = f"{screen}/{widget_id}" if screen else widget_id
            source = "widget"
        elif menu_index is not None:
            base = f"{screen or 'choice'}/item/{menu_index}"
            source = "menu"
        elif drag_name:
            base = f"{screen or 'screen'}/{drag_name}"
            source = "drag"
        elif action:
            base = f"{screen or 'screen'}/{action}"
            source = "action"
        elif image:
            base = f"{screen or 'screen'}/{image}"
            source = "image"
        else:
            ordinal = _menu_index(element.get("ordinal"))
            if ordinal is None:
                ordinal = fallback
            base = f"{screen or 'screen'}/{role}/{ordinal}"
            source = "synthetic"
            synthetic = True
        if source in {"action", "drag"}:
            split_counts[base] = split_counts.get(base, 0) + 1
        drafts.append((base, source, image, screen, role, synthetic, element))

    used: dict[str, int] = {}
    assigned: list[dict[str, Any]] = []
    for base, source, image, screen, role, synthetic, element in drafts:
        if source in {"action", "drag"} and split_counts.get(base, 0) > 1 and image:
            base = f"{base}/{image}"
        count = used.get(base, 0)
        used[base] = count + 1
        control_id = base if count == 0 else f"{base}#{count + 1}"
        record = dict(element)
        record["id"] = control_id
        record["role"] = role
        record["screen"] = screen
        record["image"] = image
        record["operations"] = operations_for(role)
        record["synthetic"] = synthetic
        assigned.append(record)
    return assigned


def _is_chrome(screen: str | None, overlay_screens: set[str]) -> bool:
    if not screen:
        return False
    return screen in overlay_screens or screen.startswith("_renforge_")


def normalize_say_dismiss(value: Any) -> str | None:
    names: list[str] = []
    if isinstance(value, str):
        names = [value]
    elif isinstance(value, (list, tuple, set)):
        names = [item for item in value if isinstance(item, str)]
    found = set(names)
    if "dismiss" in found:
        return "dismiss"
    if "dismiss_hard_pause" in found:
        return "dismiss_hard_pause"
    return None


def _interaction(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    return default


def _real_number(value: Any) -> bool:
    return not isinstance(value, bool) and isinstance(value, (int, float))


def _public_axis(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    axis = {
        key: value.get(key) if _real_number(value.get(key)) else None
        for key in ("value", "range", "step", "page")
    }
    if all(item is None for item in axis.values()):
        return None
    return axis


def _public_adjustment(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    if any(key in value for key in ("value", "range", "step", "page")):
        return _public_axis(value)
    axes = {}
    for name in ("x", "y"):
        axis = _public_axis(value.get(name))
        if axis is not None:
            axes[name] = axis
    return axes or None


def _public_control(control: Mapping[str, Any]) -> dict[str, Any]:
    enabled = _bool(control.get("enabled"), True)
    covered = _bool(control.get("covered"), False)
    clickable = control.get("clickable")
    if not isinstance(clickable, bool):
        clickable = enabled and not covered
    bounds = control.get("bounds") if isinstance(control.get("bounds"), dict) else None
    action = control.get("action") if isinstance(control.get("action"), str) else None
    text = control.get("text") if isinstance(control.get("text"), str) else None
    image = control.get("image") if isinstance(control.get("image"), str) else None
    alternate = control.get("alternate") if isinstance(control.get("alternate"), str) else None
    hovered = control.get("hovered") if isinstance(control.get("hovered"), str) else None
    selected = control.get("selected") if isinstance(control.get("selected"), bool) else None
    return {
        "id": control.get("id"),
        "role": control.get("role"),
        "text": text or None,
        "screen": control.get("screen"),
        "action": action,
        "image": image or None,
        "alternate": alternate or None,
        "hovered": hovered or None,
        "selected": selected,
        "adjustment": _public_adjustment(control.get("adjustment")),
        "operations": list(control.get("operations") or []),
        "enabled": enabled,
        "clickable": clickable,
        "covered": covered,
        "bounds": bounds,
        "synthetic": bool(control.get("synthetic")),
    }


def _is_decision(control: Mapping[str, Any]) -> bool:
    if not control.get("enabled", True):
        return False
    if control.get("covered"):
        return False
    if control.get("clickable") is False:
        return False
    bounds = control.get("bounds")
    if isinstance(bounds, dict):
        width = bounds.get("width")
        height = bounds.get("height")
        if isinstance(width, int) and not isinstance(width, bool) and width <= 0:
            return False
        if isinstance(height, int) and not isinstance(height, bool) and height <= 0:
            return False
    return True


def _screens(raw: Mapping[str, Any], overlay_screens: set[str]) -> list[dict[str, Any]]:
    screens = []
    seen: set[tuple[str, str | None]] = set()
    for item in raw.get("screens") or []:
        if not isinstance(item, Mapping):
            continue
        name = _screen_name(item.get("name"))
        if not name:
            continue
        layer = item.get("layer") if isinstance(item.get("layer"), str) else None
        key = (name, layer)
        if key in seen:
            continue
        seen.add(key)
        screens.append(
            {
                "name": name,
                "layer": layer,
                "modal": item.get("modal") is True,
                "overlay": _is_chrome(name, overlay_screens),
            }
        )
    return screens


def strip_text_tags(value: str) -> str:
    """Remove Ren'Py say tags, leaving the words the player would read.

    ``{p}`` becomes a newline. ``{{`` and ``}}`` stay as literal braces.
    The bridge applies the same rules, using ``renpy.text.extras.filter_text_tags``
    when that function is available.
    """
    if not isinstance(value, str) or not value:
        return ""
    protected = value.replace("{{", "\ue000").replace("}}", "\ue001")
    out: list[str] = []
    index = 0
    length = len(protected)
    while index < length:
        start = protected.find("{", index)
        if start < 0:
            out.append(protected[index:])
            break
        out.append(protected[index:start])
        end = protected.find("}", start + 1)
        if end < 0:
            out.append(protected[start:])
            break
        body = protected[start + 1 : end]
        kind = body[1:] if body.startswith("/") else body
        if kind.startswith("="):
            kind = ""
        else:
            kind = kind.split("=", 1)[0].split(":", 1)[0]
        tag = False
        if body.startswith("="):
            tag = True
        elif kind:
            probe = kind[1:] if kind.startswith("#") else kind
            tag = bool(probe) and probe.replace("_", "a").isalnum() and not probe[0].isdigit()
        if not tag:
            out.append(protected[start : end + 1])
        elif body == "p":
            out.append("\n")
        index = end + 1
    return "".join(out).replace("\ue000", "{").replace("\ue001", "}").strip()


def _dialogue(raw: Mapping[str, Any]) -> dict[str, Any] | None:
    dialogue = raw.get("dialogue")
    if not isinstance(dialogue, Mapping):
        return None
    who = dialogue.get("who") if isinstance(dialogue.get("who"), str) else None
    what = dialogue.get("what") if isinstance(dialogue.get("what"), str) else None
    if isinstance(what, str):
        what = strip_text_tags(what) or None
    if who is None and what is None:
        return None
    return {"who": who, "what": what}


def _readout(raw: Mapping[str, Any]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for item in raw.get("readout") or []:
        if len(records) >= READOUT_LIMIT:
            break
        if not isinstance(item, Mapping):
            continue
        text = item.get("text")
        if not isinstance(text, str):
            continue
        text = strip_text_tags(text)
        if not text:
            continue
        if len(text) > READOUT_TEXT_LIMIT:
            text = text[: READOUT_TEXT_LIMIT - 3] + "..."
        screen = item.get("screen") if isinstance(item.get("screen"), str) and item.get("screen") else None
        record: dict[str, Any] = {"screen": screen, "text": text}
        bounds = item.get("bounds")
        if isinstance(bounds, Mapping):
            cleaned: dict[str, int] = {}
            valid = True
            for key in ("x", "y", "width", "height"):
                value = bounds.get(key)
                if isinstance(value, bool) or not isinstance(value, int):
                    valid = False
                    break
                cleaned[key] = value
            if valid:
                record["bounds"] = cleaned
        records.append(record)
    return records


def _statement(raw: Mapping[str, Any]) -> dict[str, Any] | None:
    statement = raw.get("statement")
    if not isinstance(statement, Mapping):
        return None
    file_name = statement.get("file") if isinstance(statement.get("file"), str) else None
    line = statement.get("line")
    if isinstance(line, bool) or not isinstance(line, int):
        line = None
    if file_name is None and line is None:
        return None
    return {"file": file_name, "line": line}


def classify(raw: Mapping[str, Any]) -> dict[str, Any]:
    """Turn bridge records into the snapshot ``act`` and ``advance_until`` share."""
    overlay_screens = {
        name
        for name in (raw.get("overlay_screens") or [])
        if isinstance(name, str) and name
    }
    screens = _screens(raw, overlay_screens)
    elements = [item for item in (raw.get("elements") or []) if isinstance(item, Mapping)]
    controls = []
    chrome = []
    for control in (_public_control(item) for item in assign_control_ids(elements)):
        if _is_chrome(control.get("screen"), overlay_screens):
            chrome.append(control)
        else:
            controls.append(control)

    say_dismiss = normalize_say_dismiss(raw.get("say_dismiss"))
    modal = any(screen["modal"] for screen in screens)
    decisions = [control for control in controls if _is_decision(control)]
    # A text field is not in Ren'Py's focus list and does not listen for dismiss.
    # Dismiss must not skip past it.
    text_waiting = any("text" in (control.get("operations") or []) for control in decisions)
    if modal or text_waiting:
        forward = "choose"
    elif say_dismiss == "dismiss_hard_pause":
        forward = "choose" if decisions else "wait"
    elif say_dismiss == "dismiss":
        forward = "dismiss"
    elif decisions:
        forward = "choose"
    else:
        forward = "none"

    stable = raw.get("stable") is True
    reason = raw.get("unstable_reason")
    if reason not in {"transition", "not_ready"}:
        reason = None
    if stable:
        reason = None
    elif reason is None:
        reason = "not_ready"

    omitted = raw.get("omitted") if isinstance(raw.get("omitted"), Mapping) else {}
    label = raw.get("label") if isinstance(raw.get("label"), str) else None
    frame_hash = raw.get("frame_hash") if isinstance(raw.get("frame_hash"), str) else None
    return {
        "interaction": _interaction(raw.get("interaction")),
        "stable": stable,
        "unstable_reason": reason,
        "statement": _statement(raw),
        "label": label,
        "dialogue": _dialogue(raw),
        "screens": screens,
        "say_dismiss": say_dismiss,
        "forward": forward,
        "controls": controls,
        "chrome": chrome,
        "readout": _readout(raw),
        "frame_hash": frame_hash or None,
        "omitted": {
            "focus_truncated": bool(omitted.get("focus_truncated")),
            "unclassified": bool(omitted.get("unclassified")),
        },
    }


def _find_control(snapshot: Mapping[str, Any], control_id: str) -> dict[str, Any] | None:
    for bucket in ("controls", "chrome"):
        for item in snapshot.get(bucket) or []:
            if isinstance(item, Mapping) and item.get("id") == control_id:
                return dict(item)
    return None


def guard_act(
    snapshot: Mapping[str, Any],
    interaction: Any,
    control_id: str,
    *,
    text: str | None = None,
    value: Any = None,
    x: Any = None,
    y: Any = None,
    drop: str | None = None,
) -> str | None:
    """Return a refusal, or None when the bridge may post this control's input.

    ``stable`` false is never a legal token, even when ``interaction`` matches.
    """
    if snapshot.get("stable") is not True:
        return "unstable"
    if snapshot.get("interaction") != _interaction(interaction):
        return "stale"
    found = _find_control(snapshot, control_id)
    if found is None:
        return "missing"
    if not found.get("enabled", True):
        return "disabled"
    if found.get("covered") or found.get("clickable") is False:
        return "covered"
    operations = found.get("operations") or []
    if "click" in operations:
        return None
    if "text" in operations:
        if not isinstance(text, str):
            return "text_required"
        return None
    if "value" in operations:
        if found.get("role") == "viewport":
            if x is None and y is None:
                return "value_required"
            if x is not None and not _real_number(x):
                return "value_required"
            if y is not None and not _real_number(y):
                return "value_required"
            return None
        if not _real_number(value):
            return "value_required"
        return None
    if "drop" in operations:
        if drop is not None and not isinstance(drop, str):
            return "unsupported"
        return None
    return "unsupported"


def guard_dismiss(snapshot: Mapping[str, Any], interaction: Any) -> str | None:
    """Return a refusal, or None when ``queue_event('dismiss')`` is legal."""
    if snapshot.get("stable") is not True:
        return "unstable"
    if snapshot.get("interaction") != _interaction(interaction):
        return "stale"
    modal = any(
        isinstance(screen, Mapping) and screen.get("modal") is True
        for screen in snapshot.get("screens") or []
    )
    if snapshot.get("say_dismiss") != "dismiss" or modal:
        return "not_dismiss"
    for control in snapshot.get("controls") or []:
        if not isinstance(control, Mapping):
            continue
        if "text" not in (control.get("operations") or []):
            continue
        if _is_decision(control):
            return "not_dismiss"
    return None


def apply_screenshot(raw: Mapping[str, Any]) -> tuple[dict[str, Any], bytes | None]:
    """Drop bridge image bytes and set ``frame_hash`` from those exact bytes."""
    data = dict(raw)
    encoded = data.pop("screenshot_base64", None)
    data.pop("sha256", None)
    if encoded is None:
        return data, None
    if not isinstance(encoded, str) or not encoded:
        raise ValueError("screenshot_base64 must be a non-empty string")
    png = base64.b64decode(encoded, validate=True)
    data["frame_hash"] = hashlib.sha256(png).hexdigest()
    return data, png


def _finished(stop: str, steps: int, observation: dict[str, Any] | None) -> dict[str, Any]:
    return {
        "ok": True,
        "stop": stop if stop in _STOPS else "crash",
        "steps": steps,
        "observation": observation,
    }


def run_advance_until(
    fetch: Callable[[bool], Mapping[str, Any]],
    dismiss: Callable[[int], Mapping[str, Any]],
    *,
    max_steps: int,
    timeout: float,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], float] = time.monotonic,
) -> dict[str, Any]:
    """Post ``dismiss`` until the snapshot says the player must choose.

    ``fetch(screenshot)`` returns raw bridge records. ``dismiss(interaction)``
    re-checks on the main thread and posts the event only when it still listens
    for ``dismiss``. This loop never clicks and never enables skip.
    """
    if isinstance(max_steps, bool) or not isinstance(max_steps, int) or not 1 <= max_steps <= 200:
        return {
            "ok": False,
            "stop": "crash",
            "steps": 0,
            "observation": None,
            "error": "max_steps must be an integer from 1 to 200",
        }
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
        timeout_ok = False
    else:
        timeout_ok = timeout >= 0.1 and timeout <= 120
    if not timeout_ok:
        return {
            "ok": False,
            "stop": "crash",
            "steps": 0,
            "observation": None,
            "error": "timeout must be a number from 0.1 to 120 seconds",
        }

    deadline = now() + float(timeout)
    steps = 0
    last: dict[str, Any] | None = None

    def look(screenshot: bool) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        nonlocal last
        raw = fetch(screenshot)
        if not isinstance(raw, Mapping) or raw.get("ok") is False:
            error = raw.get("error") if isinstance(raw, Mapping) else "observe failed"
            return None, {
                "ok": False,
                "stop": "crash",
                "steps": steps,
                "observation": last,
                "error": error if isinstance(error, str) else "observe failed",
            }
        snapshot = classify(raw)
        last = snapshot
        return snapshot, None

    def shot(fallback: dict[str, Any] | None) -> dict[str, Any] | None:
        snapshot, failure = look(True)
        if failure is not None:
            return fallback
        return snapshot

    while True:
        if now() >= deadline:
            return _finished("timeout", steps, shot(last))
        snapshot, failure = look(False)
        if failure is not None:
            return failure
        assert snapshot is not None
        if not snapshot["stable"]:
            sleep(POLL_SECONDS)
            continue
        forward = snapshot["forward"]
        if forward == "wait":
            seen = snapshot["interaction"]
            changed = False
            while now() < deadline:
                sleep(POLL_SECONDS)
                nxt, failure = look(False)
                if failure is not None:
                    return failure
                assert nxt is not None
                if nxt["interaction"] != seen:
                    changed = True
                    break
            if not changed:
                return _finished("wait", steps, shot(snapshot))
            continue
        if forward != "dismiss":
            framed, failure = look(True)
            if failure is not None:
                return failure
            assert framed is not None
            if framed["forward"] == "dismiss" and framed["stable"]:
                snapshot = framed
            else:
                stop = framed["forward"]
                if stop not in {"choose", "wait", "none"}:
                    stop = "none"
                return _finished(stop, steps, framed)
        if snapshot["forward"] != "dismiss" or not snapshot["stable"]:
            continue
        if steps >= max_steps:
            return _finished("max_steps", steps, shot(snapshot))
        interaction = snapshot["interaction"]
        if not isinstance(interaction, int):
            return _finished("none", steps, shot(snapshot))
        reply = dismiss(interaction)
        if not isinstance(reply, Mapping) or reply.get("ok") is False:
            error = reply.get("error") if isinstance(reply, Mapping) else None
            if error in {"unstable", "stale", "not_dismiss"}:
                sleep(POLL_SECONDS)
                continue
            return {
                "ok": False,
                "stop": "crash",
                "steps": steps,
                "observation": snapshot,
                "error": error if isinstance(error, str) else "dismiss failed",
            }
        steps += 1
        advanced = False
        while now() < deadline:
            sleep(POLL_SECONDS)
            nxt, failure = look(False)
            if failure is not None:
                return failure
            assert nxt is not None
            if nxt["interaction"] != interaction:
                advanced = True
                break
        if not advanced:
            return _finished("stalled", steps, shot(snapshot))
