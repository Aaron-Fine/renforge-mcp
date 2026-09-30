"""Classifier tests for observe, act, and advance_until. No Ren'Py process."""

from __future__ import annotations

import base64
import hashlib

import pytest

from renforge.observe import (
    apply_screenshot,
    classify,
    guard_act,
    guard_dismiss,
    run_advance_until,
)


def _element(**overrides):
    element = {
        "screen": "say",
        "role": "button",
        "text": None,
        "enabled": True,
        "clickable": True,
        "covered": False,
        "widget_id": None,
        "image_name": None,
        "menu_index": None,
        "ordinal": 0,
        "action": "Return",
        "bounds": {"x": 10, "y": 20, "width": 40, "height": 16},
    }
    element.update(overrides)
    return element


def _raw(**overrides):
    raw = {
        "interaction": 4,
        "stable": True,
        "unstable_reason": None,
        "statement": {"file": "game/script.rpy", "line": 22},
        "label": "start",
        "dialogue": {"who": None, "what": "Hello"},
        "screens": [{"name": "say", "layer": "screens", "modal": False}],
        "say_dismiss": "dismiss",
        "overlay_screens": ["quick_menu"],
        "elements": [],
        "omitted": {"focus_truncated": False, "unclassified": False},
        "frame_hash": None,
    }
    raw.update(overrides)
    return raw


def _ids(snapshot, bucket="controls"):
    return [item["id"] for item in snapshot[bucket]]


def test_say_with_quick_menu_is_dismiss_and_chrome():
    snapshot = classify(
        _raw(
            elements=[
                _element(screen="quick_menu", text="Skip", action="Skip", ordinal=1),
                _element(screen="quick_menu", text="Save", action="ShowMenu", ordinal=2),
            ]
        )
    )

    assert snapshot["forward"] == "dismiss"
    assert snapshot["controls"] == []
    assert [item["text"] for item in snapshot["chrome"]] == ["Skip", "Save"]
    assert guard_dismiss(snapshot, 4) is None


def test_menu_items_choose_with_caption_ids():
    snapshot = classify(
        _raw(
            say_dismiss=None,
            dialogue=None,
            screens=[{"name": "choice", "layer": "screens", "modal": True}],
            elements=[
                _element(screen="choice", role="textbutton", text="Left", menu_index=0, ordinal=0),
                _element(screen="choice", role="button", text="Right", menu_index=1, ordinal=1),
            ],
        )
    )

    assert snapshot["forward"] == "choose"
    assert _ids(snapshot) == ["choice/item/0", "choice/item/1"]
    assert [item["text"] for item in snapshot["controls"]] == ["Left", "Right"]
    assert guard_dismiss(snapshot, 4) == "not_dismiss"


def test_hard_pause_imagebuttons_choose_by_idle_image_name():
    snapshot = classify(
        _raw(
            say_dismiss="dismiss_hard_pause",
            label="cabins",
            screens=[{"name": "freeroam_cabins", "layer": "master", "modal": False}],
            elements=[
                _element(
                    screen="freeroam_cabins",
                    role="imagebutton",
                    text=None,
                    widget_id=None,
                    image_name="images/button_cabin_door_1.png",
                    action="Function",
                    ordinal=3,
                )
            ],
        )
    )

    assert snapshot["forward"] == "choose"
    assert snapshot["screens"][0]["modal"] is False
    assert _ids(snapshot) == ["freeroam_cabins/button_cabin_door_1.png"]
    assert snapshot["controls"][0]["synthetic"] is False
    assert snapshot["controls"][0]["action"] == "Function"
    assert guard_dismiss(snapshot, 4) == "not_dismiss"


def test_text_field_stops_dismiss_even_when_say_is_listening():
    snapshot = classify(
        _raw(
            say_dismiss="dismiss",
            elements=[
                _element(
                    screen="input",
                    role="input",
                    text="First name...",
                    widget_id="input",
                    action=None,
                )
            ],
        )
    )

    assert snapshot["forward"] == "choose"
    assert snapshot["controls"][0]["id"] == "input/input"
    assert snapshot["controls"][0]["operations"] == ["text"]
    assert guard_act(snapshot, 4, "input/input", text="Nathan") is None
    assert guard_act(snapshot, 4, "input/input") == "text_required"
    assert guard_dismiss(snapshot, 4) == "not_dismiss"


def test_hard_pause_without_decisions_is_wait_and_not_dismissed():
    snapshot = classify(
        _raw(
            say_dismiss="dismiss_hard_pause",
            elements=[_element(screen="quick_menu", text="Skip", action="Skip")],
        )
    )

    assert snapshot["forward"] == "wait"
    assert snapshot["controls"] == []
    assert snapshot["chrome"][0]["text"] == "Skip"
    assert guard_dismiss(snapshot, 4) == "not_dismiss"


def test_modal_call_screen_chooses_without_say_behavior():
    snapshot = classify(
        _raw(
            say_dismiss=None,
            dialogue=None,
            screens=[{"name": "codex", "layer": "screens", "modal": True}],
            elements=[
                _element(
                    screen="codex",
                    role="imagebutton",
                    widget_id="codex_close",
                    image_name="gui/close.png",
                )
            ],
        )
    )

    assert snapshot["forward"] == "choose"
    assert _ids(snapshot) == ["codex/codex_close"]


def test_imagebutton_and_hotspot_are_clickable_buttons():
    snapshot = classify(
        _raw(
            say_dismiss=None,
            screens=[{"name": "map", "layer": "master", "modal": True}],
            elements=[
                _element(screen="map", role="imagebutton", widget_id="door"),
                _element(screen="map", role="hotspot", widget_id="path", ordinal=1),
            ],
        )
    )

    assert [(item["role"], item["operations"]) for item in snapshot["controls"]] == [
        ("button", ["click"]),
        ("button", ["click"]),
    ]


def test_bar_or_drag_on_a_hub_chooses_with_empty_operations():
    snapshot = classify(
        _raw(
            say_dismiss="dismiss_hard_pause",
            screens=[{"name": "puzzle", "layer": "master", "modal": False}],
            elements=[
                _element(screen="puzzle", role="bar", widget_id="volume", action="Preference"),
                _element(screen="puzzle", role="drag", widget_id="token", action="Drag", ordinal=1),
            ],
        )
    )

    assert snapshot["forward"] == "choose"
    assert [item["operations"] for item in snapshot["controls"]] == [[], []]
    assert guard_act(snapshot, 4, "puzzle/volume") == "unsupported"


def test_unstable_snapshot_is_not_an_act_token():
    snapshot = classify(
        _raw(
            stable=False,
            unstable_reason="transition",
            elements=[_element(screen="choice", widget_id="go", menu_index=0)],
            screens=[{"name": "choice", "layer": "screens", "modal": True}],
        )
    )

    assert snapshot["stable"] is False
    assert snapshot["unstable_reason"] == "transition"
    assert guard_act(snapshot, snapshot["interaction"], snapshot["controls"][0]["id"]) == "unstable"
    assert guard_dismiss(snapshot, snapshot["interaction"]) == "unstable"


def test_say_with_non_modal_hotspot_stays_dismiss():
    snapshot = classify(
        _raw(
            say_dismiss="dismiss",
            screens=[
                {"name": "say", "layer": "screens", "modal": False},
                {"name": "shrine", "layer": "master", "modal": False},
            ],
            elements=[_element(screen="shrine", role="hotspot", widget_id="touch")],
        )
    )

    assert snapshot["forward"] == "dismiss"
    assert _ids(snapshot) == ["shrine/touch"]
    assert guard_dismiss(snapshot, 4) is None


def test_disabled_hard_pause_control_does_not_force_choose():
    snapshot = classify(
        _raw(
            say_dismiss="dismiss_hard_pause",
            screens=[{"name": "map", "layer": "master", "modal": False}],
            elements=[_element(screen="map", widget_id="door", enabled=False, clickable=False)],
        )
    )

    assert snapshot["forward"] == "wait"
    assert snapshot["controls"][0]["id"] == "map/door"
    assert guard_act(snapshot, 4, "map/door") == "disabled"


def test_duplicate_image_names_gain_a_suffix():
    snapshot = classify(
        _raw(
            say_dismiss="dismiss_hard_pause",
            screens=[{"name": "map", "layer": "master", "modal": False}],
            elements=[
                _element(screen="map", role="imagebutton", image_name="door.png", ordinal=0),
                _element(screen="map", role="imagebutton", image_name="images/door.png", ordinal=1),
            ],
        )
    )

    assert _ids(snapshot) == ["map/door.png", "map/door.png#2"]


def test_modal_say_stops_kinetic_advance():
    snapshot = classify(
        _raw(screens=[{"name": "say", "layer": "screens", "modal": True}])
    )

    assert snapshot["forward"] == "choose"
    assert snapshot["say_dismiss"] == "dismiss"


def test_dismiss_wins_when_both_say_events_are_present():
    snapshot = classify(_raw(say_dismiss=["dismiss_hard_pause", "dismiss"]))

    assert snapshot["say_dismiss"] == "dismiss"
    assert snapshot["forward"] == "dismiss"


def test_screenshot_hash_matches_the_attached_bytes():
    png = b"\x89PNG\r\n\x1a\n" + b"frame"
    raw, attached = apply_screenshot(
        _raw(screenshot_base64=base64.b64encode(png).decode("ascii"), sha256="stale")
    )

    assert attached == png
    assert raw["frame_hash"] == hashlib.sha256(png).hexdigest()
    assert "screenshot_base64" not in raw
    assert classify(raw)["frame_hash"] == hashlib.sha256(png).hexdigest()


def test_stale_covered_and_missing_act_guards():
    snapshot = classify(
        _raw(
            elements=[
                _element(screen="map", widget_id="door", covered=True, clickable=False),
            ],
            screens=[{"name": "map", "layer": "master", "modal": True}],
        )
    )

    assert guard_act(snapshot, 3, "map/door") == "stale"
    assert guard_act(snapshot, 4, "map/missing") == "missing"
    assert guard_act(snapshot, 4, "map/door") == "covered"


class _Clock:
    def __init__(self):
        self.t = 0.0

    def now(self):
        return self.t

    def sleep(self, delay):
        self.t += delay


def _run(frames, *, max_steps=5, timeout=2.0, advance_on_dismiss=True):
    clock = _Clock()
    index = {"i": 0}
    dismisses = []

    def fetch(_screenshot):
        return frames[min(index["i"], len(frames) - 1)]

    def dismiss(interaction):
        dismisses.append(interaction)
        if advance_on_dismiss and index["i"] + 1 < len(frames):
            index["i"] += 1
        return {"ok": True}

    result = run_advance_until(
        fetch,
        dismiss,
        max_steps=max_steps,
        timeout=timeout,
        sleep=clock.sleep,
        now=clock.now,
    )
    return result, dismisses


def test_advance_until_dismisses_say_and_stops_on_choice_without_clicking():
    say = _raw(interaction=1, say_dismiss="dismiss")
    choice = _raw(
        interaction=2,
        say_dismiss=None,
        screens=[{"name": "choice", "layer": "screens", "modal": True}],
        elements=[_element(screen="choice", text="Left", menu_index=0)],
    )

    result, dismisses = _run([say, choice])

    assert result["stop"] == "choose"
    assert result["steps"] == 1
    assert dismisses == [1]
    assert result["observation"]["controls"][0]["id"] == "choice/item/0"


def test_advance_until_polls_through_wait_and_does_not_dismiss_it():
    waiting = _raw(interaction=1, say_dismiss="dismiss_hard_pause", elements=[])
    say = _raw(interaction=2, say_dismiss="dismiss")
    choice = _raw(
        interaction=3,
        say_dismiss=None,
        screens=[{"name": "choice", "layer": "screens", "modal": True}],
        elements=[_element(screen="choice", text="Go", menu_index=0)],
    )
    clock = _Clock()
    phase = {"name": "wait"}
    dismisses = []

    def fetch(_screenshot):
        if phase["name"] == "wait" and clock.t >= 0.1:
            phase["name"] = "say"
        if phase["name"] == "wait":
            return waiting
        if phase["name"] == "say":
            return say
        return choice

    def dismiss(interaction):
        dismisses.append(interaction)
        phase["name"] = "choice"
        return {"ok": True}

    result = run_advance_until(
        fetch,
        dismiss,
        max_steps=5,
        timeout=2,
        sleep=clock.sleep,
        now=clock.now,
    )

    assert result["stop"] == "choose"
    assert dismisses == [2]
    assert result["steps"] == 1


def test_advance_until_does_not_dismiss_a_hard_pause_hub():
    hub = _raw(
        interaction=7,
        say_dismiss="dismiss_hard_pause",
        screens=[{"name": "map", "layer": "master", "modal": False}],
        elements=[_element(screen="map", role="imagebutton", image_name="door.png")],
    )

    result, dismisses = _run([hub])

    assert result["stop"] == "choose"
    assert result["steps"] == 0
    assert dismisses == []
    assert result["observation"]["controls"][0]["id"] == "map/door.png"


def test_advance_until_reports_stalled_when_dismiss_does_not_advance():
    say = _raw(interaction=1, say_dismiss="dismiss")

    result, dismisses = _run([say], advance_on_dismiss=False, timeout=0.2)

    assert result["stop"] == "stalled"
    assert result["steps"] == 1
    assert dismisses == [1]


def test_advance_until_stops_at_max_steps_without_another_dismiss():
    frames = [
        _raw(interaction=1, say_dismiss="dismiss"),
        _raw(interaction=2, say_dismiss="dismiss"),
        _raw(
            interaction=3,
            say_dismiss=None,
            screens=[{"name": "choice", "layer": "screens", "modal": True}],
            elements=[_element(screen="choice", text="Go", menu_index=0)],
        ),
    ]

    result, dismisses = _run(frames, max_steps=1)

    assert result["stop"] == "max_steps"
    assert dismisses == [1]
    assert result["observation"]["interaction"] == 2


def test_advance_until_rejects_bad_budgets():
    result = run_advance_until(lambda _s: {}, lambda _i: {}, max_steps=0, timeout=1)

    assert result["ok"] is False
    assert result["stop"] == "crash"


def test_apply_screenshot_rejects_invalid_payload():
    with pytest.raises(ValueError):
        apply_screenshot(_raw(screenshot_base64="not-base64"))
