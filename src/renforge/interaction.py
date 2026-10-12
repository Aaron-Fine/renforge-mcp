"""Pure gameplay contract shared by the host and the injected Ren'Py bridge.

Only standard-library imports belong here. The launcher embeds this source in
an ordinary Python module, outside Ren'Py store/rollback, so discovery, ids,
classification and guards have exactly one implementation on both sides.
"""
from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any

READOUT_LIMIT = 64
READOUT_TEXT_LIMIT = 400


def validate_frame(frame: Mapping[str, Any], *, session_id: Any,
                   logical_size: tuple[Any, Any], source_size: tuple[Any, Any]) -> str | None:
    """Validate a delivered-image descriptor before posting coordinate input."""
    for key in ("width", "height", "original_width", "original_height", "logical_width", "logical_height"):
        value = frame.get(key)
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            return "invalid frame dimensions"
    if not isinstance(frame.get("frame_id"), str) or not frame["frame_id"]:
        return "invalid frame identity"
    if not session_id or frame.get("session_id") != session_id:
        return "frame session guard failed"
    if (frame["logical_width"], frame["logical_height"]) != logical_size:
        return "frame logical dimensions guard failed"
    if (frame["original_width"], frame["original_height"]) != source_size:
        return "frame capture dimensions guard failed"
    return None

_BUTTON_ROLES = {"button", "imagebutton", "textbutton", "hotspot", "imagemap"}
_BAR_ROLES = {"bar", "slider"}
_TEXT_ROLES = {"input"}
_EMPTY_ROLES = {"viewport", "drag", "draggroup"}


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
        record["operations"] = list(element["operations"]) if isinstance(element.get("operations"), (list, tuple)) else operations_for(role)
        record["synthetic"] = synthetic
        assigned.append(record)
    return assigned


def _is_chrome(screen: str | None) -> bool:
    if not screen:
        return False
    return screen == "quick_menu" or screen.startswith("_renforge_")


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
    return not isinstance(value, bool) and (isinstance(value, int) or isinstance(value, float) and math.isfinite(value))


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
    covered = None if "covered" in control and control["covered"] is None else _bool(control.get("covered"), False)
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
        "draggable": control.get("draggable"),
        "droppable": control.get("droppable"),
        "operations": list(control.get("operations") or []),
        "enabled": enabled,
        "clickable": clickable,
        "covered": covered,
        "bounds": bounds,
        "visible": control.get("visible"),
        "bounds_reason": control.get("bounds_reason"),
        "coverage_reason": control.get("coverage_reason"),
        "click_point": control.get("click_point"),
        "semantic": bool(control.get("semantic")),
        "occurrences": control.get("occurrences") or [],
        "layer": control.get("layer"),
        "synthetic": bool(control.get("synthetic")),
        "source": control.get("source") if isinstance(control.get("source"), str) else None,
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
                "overlay": name in overlay_screens or name.startswith("_renforge_"),
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


def crossed_transcript(events: list[Any], frames: list[Any]) -> list[dict[str, Any]]:
    """Label changes and say lines, in the order the player reached them.

    ``events`` are bridge label and say records with ``seq``. ``frames`` are
    the stable snapshots ``advance_until`` already read, each stamped with
    ``event_seq``. A say event and a frame that show the same line are one
    entry. The label is recorded only when it changes. Text tags are removed.
    """
    ordered = [
        event
        for event in events
        if isinstance(event, Mapping) and event.get("type") in {"label", "say"}
    ]
    ordered.sort(key=lambda event: event.get("seq") if isinstance(event.get("seq"), int) and not isinstance(event.get("seq"), bool) else 0)
    notes: list[dict[str, Any]] = []
    last_label: str | None = None
    last_say: tuple[str | None, str] | None = None
    seen_occurrences: dict[Any, int] = {}
    index = 0

    def emit_label(name: Any) -> None:
        nonlocal last_label
        # Ren'Py's own labels (_start, _main_menu, _gl_test) are not sections
        # the player entered. A game label may still be named main_menu.
        if not isinstance(name, str) or not name or name.startswith("_") or name == last_label:
            return
        last_label = name
        notes.append({"type": "label", "label": name})

    def emit_say(who: Any, what: Any, *, occurrence: Any = None, event: bool = False,
                 seen: Any = None) -> None:
        nonlocal last_say
        if not isinstance(what, str):
            return
        text = strip_text_tags(what)
        if not text:
            return
        speaker = who.strip() if isinstance(who, str) and who.strip() else None
        key = (speaker, text)
        if occurrence is not None:
            if occurrence in seen_occurrences:
                # Older engines queue the un-interpolated source line. The
                # rendered snapshot is authoritative for that same occurrence.
                if not event:
                    notes[seen_occurrences[occurrence]].update({"who": speaker, "what": text})
                    last_say = key
                return
            seen_occurrences[occurrence] = len(notes)
        elif not event and key == last_say:
            return
        last_say = key
        note = {"type": "say", "who": speaker, "what": text}
        if seen is True:
            note["seen"] = True  # already read once since launch: a repeat
        notes.append(note)

    def drain(through: int) -> None:
        nonlocal index
        while index < len(ordered):
            event = ordered[index]
            seq = event.get("seq")
            if isinstance(seq, bool) or not isinstance(seq, int) or seq > through:
                break
            index += 1
            if event.get("type") == "label":
                emit_label(event.get("label"))
            else:
                emit_say(event.get("who"), event.get("what"), occurrence=event.get("say_id"), event=True,
                         seen=event.get("seen"))

    for frame in frames:
        if not isinstance(frame, Mapping):
            continue
        seq = frame.get("event_seq")
        if isinstance(seq, int) and not isinstance(seq, bool):
            drain(seq)
        emit_label(frame.get("label"))
        dialogue = frame.get("dialogue")
        if isinstance(dialogue, Mapping):
            emit_say(dialogue.get("who"), dialogue.get("what"), occurrence=frame.get("say_id"))
    drain(2**31 - 1)
    return notes


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
    for item in assign_control_ids(elements):
        control = _public_control(item)
        # Screen-name chrome, or controls whose actions are only Ren'Py interface
        # actions (menus, skip, rollback, saves), never story decisions.
        if _is_chrome(control.get("screen")) or item.get("chrome") is True:
            chrome.append(control)
        else:
            controls.append(control)

    say_dismiss = normalize_say_dismiss(raw.get("say_dismiss"))
    modal = any(screen["modal"] for screen in screens)
    decisions = [control for control in controls if _is_decision(control)]
    omitted = raw.get("omitted") if isinstance(raw.get("omitted"), Mapping) else {}
    if modal or decisions:
        forward = "choose"
    elif omitted.get("unclassified") or omitted.get("focus_truncated"):
        forward = "none"
    elif say_dismiss == "dismiss_hard_pause":
        forward = "wait"
    elif say_dismiss == "dismiss":
        forward = "dismiss"
    else:
        forward = "none"

    stable = raw.get("stable") is True
    reason = raw.get("unstable_reason")
    if reason not in {"transition", "not_ready", "pause"}:
        reason = None
    if stable:
        reason = None
    elif reason is None:
        reason = "not_ready"

    label = raw.get("label") if isinstance(raw.get("label"), str) else None
    frame_hash = raw.get("frame_hash") if isinstance(raw.get("frame_hash"), str) else None
    lifecycle = raw.get("lifecycle")
    lifecycle = lifecycle if isinstance(lifecycle, Mapping) else {}
    phase = lifecycle.get("phase")
    if phase not in {"unknown", "playing", "main_menu", "ended"}:
        phase = "unknown"
    ending = lifecycle.get("ending")
    ending = dict(ending) if phase == "ended" and isinstance(ending, Mapping) and ending.get("reason") == "script_return" else None
    if phase == "ended" and ending is None:
        phase = "main_menu"
    return {
        "session_id": raw.get("session_id"),
        "observation_id": raw.get("observation_id"),
        "interaction": _interaction(raw.get("interaction")),
        "stable": stable,
        "unstable_reason": reason,
        "statement": _statement(raw),
        "label": label,
        # True when this statement already finished once since launch.
        "seen": raw.get("seen") if isinstance(raw.get("seen"), bool) else None,
        "lifecycle": {"phase": phase, "ending": ending},
        "dialogue": _dialogue(raw),
        "text_revealing": raw.get("text_revealing"),
        "screens": screens,
        "say_dismiss": say_dismiss,
        "forward": forward,
        "controls": controls,
        "chrome": chrome,
        "readout": _readout(raw),
        "input_candidates": list(raw.get("input_candidates") or []),
        "frame_hash": frame_hash or None,
        "frame_id": raw.get("frame_id"),
        "frame": raw.get("frame"),
        "screenshot": raw.get("screenshot"),
        "omitted": {
            "focus_truncated": bool(omitted.get("focus_truncated")),
            "unclassified": bool(omitted.get("unclassified")),
            "readout_truncated": bool(omitted.get("readout_truncated")),
        },
    }


def progression_boundary(snapshot: Mapping[str, Any]) -> str | None:
    """A menu is always a boundary; only engine evidence establishes an end."""
    lifecycle = snapshot.get("lifecycle") or {}
    if lifecycle.get("phase") == "ended":
        return "end"
    if lifecycle.get("phase") == "main_menu":
        return "main_menu"
    omitted = snapshot.get("omitted") or {}
    if omitted.get("unclassified") or omitted.get("focus_truncated"):
        return "unsupported"
    forward = snapshot.get("forward")
    return forward if forward in ("choose", "none") else None


def _find_control(snapshot: Mapping[str, Any], control_id: str) -> dict[str, Any] | None:
    for bucket in ("controls", "chrome"):
        for item in snapshot.get(bucket) or []:
            if isinstance(item, Mapping) and item.get("id") == control_id:
                return dict(item)
    return None


def validate_act_payload(payload: Mapping[str, Any]) -> str | None:
    """Validate every supplied field before the bridge can mutate anything."""
    # observation_id alone is enough: it already covers interaction and session.
    if payload.get("interaction") is None:
        if payload.get("observation_id") is None:
            return "stale"
    elif _interaction(payload.get("interaction")) is None:
        return "stale"
    has_id = isinstance(payload.get("id"), str) and bool(payload.get("id"))
    has_key = payload.get("key") is not None
    if has_key and (has_id or any(payload.get(name) is not None for name in ("text", "value", "x", "y", "drop"))):
        return "key_or_control"
    if not has_id and not has_key:
        return "missing"
    if payload.get("text") is not None and not isinstance(payload["text"], str):
        return "text_required"
    for name in ("value", "x", "y"):
        if payload.get(name) is not None and not _real_number(payload[name]):
            return "value_required"
    if payload.get("drop") is not None and not isinstance(payload["drop"], str):
        return "unsupported"
    if payload.get("hold") is not None and (not isinstance(payload["hold"], bool) or not has_key):
        return "unsupported"
    if "submit" in payload and not isinstance(payload["submit"], bool):
        return "unsupported"
    for name in ("session_id", "observation_id"):
        if payload.get(name) is not None and (not isinstance(payload[name], str) or not payload[name]):
            return "stale"
    return None


def guard_identity(snapshot: Mapping[str, Any], interaction: Any, *,
                   session_id: str | None = None, observation_id: str | None = None) -> str | None:
    if session_id is not None and snapshot.get("session_id") != session_id:
        return "stale"
    if observation_id is not None and snapshot.get("observation_id") != observation_id:
        return "stale"
    if snapshot.get("stable") is not True:
        return "unstable"
    if interaction is None and observation_id is not None:
        return None
    if _interaction(interaction) is None or snapshot.get("interaction") != interaction:
        return "stale"
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
    session_id: str | None = None,
    observation_id: str | None = None,
) -> str | None:
    """Return a refusal, or None when the bridge may post this control's input.

    ``stable`` false is never a legal token, even when ``interaction`` matches.
    """
    refusal = guard_identity(snapshot, interaction, session_id=session_id, observation_id=observation_id)
    if refusal:
        return refusal
    found = _find_control(snapshot, control_id)
    if found is None:
        return "missing"
    if not found.get("enabled", True):
        return "disabled"
    if found.get("covered") or found.get("clickable") is False:
        return "covered"
    operations = found.get("operations") or []
    if "click" in operations and drop is None:
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


def guard_dismiss(snapshot: Mapping[str, Any], interaction: Any, *,
                  session_id: str | None = None, observation_id: str | None = None) -> str | None:
    """Return a refusal, or None when ``queue_event('dismiss')`` is legal."""
    refusal = guard_identity(snapshot, interaction, session_id=session_id, observation_id=observation_id)
    if refusal:
        return refusal
    omitted = snapshot.get("omitted") or {}
    if omitted.get("unclassified") or omitted.get("focus_truncated"):
        return "unsupported"
    modal = any(
        isinstance(screen, Mapping) and screen.get("modal") is True
        for screen in snapshot.get("screens") or []
    )
    if snapshot.get("say_dismiss") != "dismiss" or modal:
        return "not_dismiss"
    for control in snapshot.get("controls") or []:
        if not isinstance(control, Mapping):
            continue
        if _is_decision(control):
            return "not_dismiss"
    return None
