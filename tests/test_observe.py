"""Classifier tests for observe, act, and advance_until. No Ren'Py process."""

from __future__ import annotations

import base64
import hashlib

import pytest

from renforge.observe import (
    apply_screenshot,
    classify,
    crossed_transcript,
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


def test_dialogue_what_drops_text_tags():
    shown = classify(
        _raw(dialogue={"who": "JACKIE", "what": "{alpha=0.5}{i}Hello{/i}{w=2.0}{nw}"})
    )
    assert shown["dialogue"] == {"who": "JACKIE", "what": "Hello"}

    paragraph = classify(_raw(dialogue={"who": None, "what": "One{p}Two"}))
    assert paragraph["dialogue"]["what"] == "One\nTwo"

    tags_only = classify(_raw(dialogue={"who": None, "what": "{w}{nw}"}))
    assert tags_only["dialogue"] is None

    literal = classify(_raw(dialogue={"who": None, "what": "Use {{b}} here"}))
    assert literal["dialogue"]["what"] == "Use {b} here"

    # The say window's own texts are dialogue, not readout as well.
    repeated = classify(_raw(dialogue={"who": "Adeline", "what": "A fine name."},
                             readout=[{"screen": "say", "text": "Adeline"},
                                      {"screen": "say", "text": "A fine name."},
                                      {"screen": "day_overlay", "text": "13"}]))
    assert [r["text"] for r in repeated["readout"]] == ["13"]

    # A nameless narrator voice is a name made only of tags.
    nameless = classify(_raw(dialogue={"who": "{noalt}{/noalt}", "what": "She smiles."}))
    assert nameless["dialogue"] == {"who": None, "what": "She smiles."}
    assert classify(_raw(dialogue={"who": "{b}Adeline{/b}", "what": "Hi."}))["dialogue"]["who"] == "Adeline"


def test_readout_does_not_change_forward_or_become_a_control():
    snapshot = classify(
        _raw(
            say_dismiss="dismiss",
            readout=[
                {
                    "screen": "sandbox",
                    "text": "Day 1",
                    "bounds": {"x": 1, "y": 2, "width": 80, "height": 20},
                },
                {"screen": "sandbox", "text": "{b}Evening{/b}"},
                {"screen": "sandbox", "text": "   "},
                {"screen": "quick_menu", "text": "Skip"},
            ],
            elements=[
                _element(screen="quick_menu", text="Skip", action="Skip", ordinal=1),
            ],
        )
    )

    assert snapshot["forward"] == "dismiss"
    assert snapshot["controls"] == []
    assert snapshot["readout"] == [
        {
            "screen": "sandbox",
            "text": "Day 1",
            "bounds": {"x": 1, "y": 2, "width": 80, "height": 20},
        },
        {"screen": "sandbox", "text": "Evening"},
        {"screen": "quick_menu", "text": "Skip"},
    ]
    assert guard_dismiss(snapshot, 4) is None

    capped = classify(_raw(readout=[{"screen": "notes", "text": "line %s" % index} for index in range(80)]))
    assert len(capped["readout"]) == 64
    assert capped["forward"] == "dismiss"


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
    assert _ids(snapshot) == ["freeroam_cabins/Function"]
    assert snapshot["controls"][0]["image"] == "button_cabin_door_1.png"
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
    assert snapshot["controls"][0]["image"] == "close.png"


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


def test_bar_viewport_and_drag_use_their_renpy_operations():
    snapshot = classify(
        _raw(
            say_dismiss="dismiss_hard_pause",
            screens=[{"name": "puzzle", "layer": "master", "modal": False}],
            elements=[
                _element(
                    screen="puzzle",
                    role="bar",
                    widget_id="volume",
                    action="Preference",
                    adjustment={"value": 2, "range": 10, "step": 1, "page": 2},
                    selected=True,
                ),
                _element(
                    screen="puzzle",
                    role="viewport",
                    widget_id="log",
                    action=None,
                    adjustment={"x": {"value": 0, "range": 80, "step": 8, "page": 40}, "y": {"value": 12, "range": 90, "step": 9, "page": 45}},
                    ordinal=1,
                ),
                _element(
                    screen="puzzle",
                    role="drag",
                    widget_id="token",
                    action="Drag",
                    alternate='ShowMenu("load")',
                    hovered="Function(ShowHint)",
                    tooltip="Drag the token onto the board.",
                    ordinal=2,
                ),
            ],
        )
    )

    assert snapshot["forward"] == "choose"
    assert [item["operations"] for item in snapshot["controls"]] == [["value"], ["value"], ["drop"]]
    assert snapshot["controls"][0]["adjustment"]["value"] == 2
    assert snapshot["controls"][0]["selected"] is True
    assert snapshot["controls"][1]["adjustment"]["y"]["value"] == 12
    assert snapshot["controls"][2]["alternate"] == 'ShowMenu("load")'
    assert snapshot["controls"][2]["hovered"] == "Function(ShowHint)"
    assert snapshot["controls"][2]["tooltip"] == "Drag the token onto the board."
    assert guard_act(snapshot, 4, "puzzle/volume") == "value_required"
    assert guard_act(snapshot, 4, "puzzle/volume", value=4) is None
    assert guard_act(snapshot, 4, "puzzle/volume", value=True) == "value_required"
    assert guard_act(snapshot, 4, "puzzle/log") == "value_required"
    assert guard_act(snapshot, 4, "puzzle/log", y=0) is None
    assert guard_act(snapshot, 4, "puzzle/token") is None
    assert guard_act(snapshot, 4, "puzzle/token", drop="puzzle/volume") is None


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


def test_say_with_non_modal_hotspot_stops_before_actionable_ui():
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

    assert snapshot["forward"] == "choose"
    assert _ids(snapshot) == ["shrine/touch"]
    assert guard_dismiss(snapshot, 4) == "not_dismiss"


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


def test_action_names_the_control_and_image_only_splits_siblings():
    snapshot = classify(
        _raw(
            say_dismiss="dismiss_hard_pause",
            screens=[{"name": "sandbox", "layer": "screens", "modal": False}],
            elements=[
                _element(
                    screen="sandbox",
                    role="imagebutton",
                    text=None,
                    image_name="images/button_time.png",
                    action="Function(AdvanceTime)",
                    ordinal=0,
                ),
                _element(
                    screen="sandbox",
                    role="imagebutton",
                    text=None,
                    image_name="images/button_map.png",
                    action="Function(ShowMap)",
                    ordinal=1,
                ),
                _element(
                    screen="map",
                    role="imagebutton",
                    text=None,
                    image_name="ok.png",
                    action="Return",
                    ordinal=0,
                ),
                _element(
                    screen="map",
                    role="imagebutton",
                    text=None,
                    image_name="cancel.png",
                    action="Return",
                    ordinal=1,
                ),
                _element(
                    screen="map",
                    role="imagebutton",
                    text=None,
                    image_name="door.png",
                    action="Return",
                    ordinal=2,
                ),
                _element(
                    screen="map",
                    role="imagebutton",
                    text=None,
                    image_name="images/door.png",
                    action="Return",
                    ordinal=3,
                ),
            ],
        )
    )

    assert _ids(snapshot) == [
        "sandbox/Function(AdvanceTime)",
        "sandbox/Function(ShowMap)",
        "map/Return/ok.png",
        "map/Return/cancel.png",
        "map/Return/door.png",
        "map/Return/door.png#2",
    ]
    assert [item["image"] for item in snapshot["controls"]] == [
        "button_time.png",
        "button_map.png",
        "ok.png",
        "cancel.png",
        "door.png",
        "door.png",
    ]


def test_drag_name_is_the_id_and_image_only_splits_siblings():
    snapshot = classify(
        _raw(
            say_dismiss="dismiss_hard_pause",
            screens=[{"name": "minigame", "layer": "screens", "modal": False}],
            elements=[
                _element(
                    screen="minigame",
                    role="drag",
                    text=None,
                    action="Function(Place)",
                    drag_name="towel",
                    image_name="images/towel.png",
                    ordinal=0,
                ),
                _element(
                    screen="minigame",
                    role="drag",
                    text=None,
                    action="Function(Place)",
                    drag_name="cup",
                    image_name="images/cup.png",
                    ordinal=1,
                ),
                _element(
                    screen="minigame",
                    role="drag",
                    text=None,
                    action="Function(Place)",
                    drag_name="cup",
                    image_name="images/cup_b.png",
                    ordinal=2,
                ),
            ],
        )
    )

    assert _ids(snapshot) == ["minigame/towel", "minigame/cup/cup.png", "minigame/cup/cup_b.png"]
    assert [item["image"] for item in snapshot["controls"]] == ["towel.png", "cup.png", "cup_b.png"]


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


@pytest.mark.parametrize("phase,stop", [("ended", "end"), ("main_menu", "main_menu")])
def test_dismiss_progression_stops_at_engine_menu_even_without_controls(phase, stop):
    raw = _raw(interaction=1, say_dismiss="dismiss")
    raw["lifecycle"] = {"phase": phase, "ending": {"reason": "script_return"} if phase == "ended" else None}
    result, dismisses = _run([raw])
    assert result["stop"] == stop
    assert dismisses == []


def test_screen_or_label_names_do_not_establish_ending():
    raw = _raw(interaction=1, say_dismiss="dismiss",
               screens=[{"name": "main_menu", "layer": "screens", "modal": False}])
    raw["label"] = "ending"
    assert classify(raw)["lifecycle"] == {"phase": "unknown", "ending": None}


@pytest.mark.parametrize("ready,keep_frame", [(True, True), (False, False), (False, True)])
def test_progression_requires_settled_current_frame_to_report_end(monkeypatch, ready, keep_frame):
    from types import SimpleNamespace
    from renforge.tools import live

    ending = {"id": 1, "reason": "script_return", "statement": {"file": "game/story.rpy", "line": 42}}
    final = classify(_raw(lifecycle={"phase": "ended", "ending": ending}))

    class Client:
        _config = SimpleNamespace(host="127.0.0.1", port=1, token="ending-test")

        def poll_events(self, cursor, **kwargs):
            return {"ok": True, "events": [], "cursor": cursor}

    monkeypatch.setattr(live, "run_native_skip", lambda *args, **kwargs: {
        "ok": True, "stop": "end", "observation": final, "steps": 1})
    monkeypatch.setattr(live, "settle", lambda *args, **kwargs:
                        (final, "ready", None) if ready else
                        (final if keep_frame else None, "timeout", "no frame"))
    result = live._advance_with_client("/tmp/ending-frame", Client(), max_steps=10, timeout=2, screenshot=False)
    assert result["stop"] == ("end" if ready else "timeout")
    assert result["ending"] == (ending if ready else None)
    if not ready and not keep_frame:
        assert result["observation"] is None
        assert result["last_observation"] == final


@pytest.mark.parametrize('initial_stop,unclassified,expected', [
    ('none', False, 'choose'), ('none', True, 'unsupported'), ('max_steps', False, 'max_steps')])
def test_progression_reports_settled_boundary_without_replacing_budget_stop(monkeypatch, initial_stop, unclassified, expected):
    from types import SimpleNamespace
    from renforge.tools import live

    final = classify(_raw(interaction=2, elements=[_element(screen='branch_picker', text='Next')],
                          omitted={'unclassified': unclassified}))

    class Client:
        _config = SimpleNamespace(host='127.0.0.1', port=1, token='settled-boundary')

        def poll_events(self, cursor, **kwargs):
            return {'ok': True, 'events': [], 'cursor': cursor}

    monkeypatch.setattr(live, 'run_native_skip', lambda *args, **kwargs: {
        'ok': True, 'stop': initial_stop, 'observation': classify(_raw()), 'steps': 0})
    monkeypatch.setattr(live, 'settle', lambda *args, **kwargs: (final, 'ready', None))
    result = live._advance_with_client('/tmp/settled-boundary', Client(), max_steps=10, timeout=2, screenshot=False)
    assert result['stop'] == expected
    assert result['observation'] == final
    assert result['steps'] == 0


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
    assert result["observation"]["controls"][0]["id"] == "map/Return"
    assert result["observation"]["controls"][0]["image"] == "door.png"


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


def test_crossed_transcript_keeps_labels_queued_before_the_first_frame():
    events = [
        {"seq": 0, "type": "label", "label": "_start"},
        {"seq": 1, "type": "label", "label": "advance_time"},
        {"seq": 2, "type": "label", "label": "week1_challenge1_cliff"},
        {"seq": 3, "type": "say", "who": "Chef", "what": "{b}Listen up!{/b}"},
        {"seq": 4, "type": "business", "name": "ignored"},
        {"seq": 6, "type": "label", "label": "week1_challenge1_hottub"},
        {"seq": 7, "type": "say", "who": None, "what": "Chris suddenly appears on the beach with you."},
    ]
    frames = [
        {
            "event_seq": 5,
            "label": "week1_challenge1_cliff",
            "dialogue": {"who": "Chef", "what": "{b}Listen up!{/b}"},
        },
        {
            "event_seq": 8,
            "label": "week1_challenge1_hottub",
            "dialogue": {"who": None, "what": "Chris suddenly appears on the beach with you."},
        },
    ]

    assert crossed_transcript(events, frames) == [
        {"type": "label", "label": "advance_time"},
        {"type": "label", "label": "week1_challenge1_cliff"},
        {"type": "say", "who": "Chef", "what": "Listen up!"},
        {"type": "label", "label": "week1_challenge1_hottub"},
        {"type": "say", "who": None, "what": "Chris suddenly appears on the beach with you."},
    ]


def test_crossed_transcript_records_a_line_the_callback_missed():
    frames = [
        {"event_seq": 4, "label": "start", "dialogue": {"who": None, "what": "One{p}Two"}},
        {"event_seq": 5, "label": "start", "dialogue": {"who": "Wisp", "what": "You're awake!"}},
    ]

    assert crossed_transcript([], frames) == [
        {"type": "label", "label": "start"},
        {"type": "say", "who": None, "what": "One\nTwo"},
        {"type": "say", "who": "Wisp", "what": "You're awake!"},
    ]


def test_advance_until_returns_lines_queued_before_its_first_look(monkeypatch):
    from renforge.tools import live

    frames = [
        _raw(
            interaction=1,
            label="week1_challenge1_cliff",
            dialogue={"who": "Chef", "what": "Listen up!"},
            event_seq=5,
            say_dismiss="dismiss",
        ),
        _raw(
            interaction=2,
            label="week1_challenge1_hottub",
            dialogue=None,
            event_seq=8,
            say_dismiss=None,
            screens=[{"name": "minigame_hottub", "layer": "master", "modal": True}],
            elements=[_element(screen="minigame_hottub", text="Skip")],
        ),
    ]
    index = {"i": 0}

    class Client:
        def __init__(self):
            self._config = type("Cfg", (), {"host": "127.0.0.1", "port": 9, "token": "tok"})()

        def observe(self, screenshot=False, deadline=None):
            return frames[min(index["i"], len(frames) - 1)]

        def dismiss_if(self, interaction, deadline=None):
            index["i"] += 1
            return {"ok": True}

        def poll_events(self, since=0, deadline=None):
            events = [
                {"seq": 1, "type": "label", "label": "freeroam_cabins"},
                {"seq": 3, "type": "label", "label": "advance_time"},
                {"seq": 4, "type": "label", "label": "week1_challenge1_cliff"},
                {"seq": 6, "type": "label", "label": "week1_challenge1_hottub"},
            ]
            return {"events": [event for event in events if event["seq"] > since], "cursor": 8}

    client = Client()
    monkeypatch.setattr(live, "_client", lambda _path: client)
    live._crossed_after[live._key("/tmp/renforge-crossed")] = (("127.0.0.1", 9, "tok"), 2)

    result = live.advance_until("/tmp/renforge-crossed", max_steps=5, timeout=2, mode="dismiss")

    assert result["stop"] == "choose"
    assert result["steps"] == 1
    assert result["crossed"] == [
        {"type": "label", "label": "advance_time"},
        {"type": "label", "label": "week1_challenge1_cliff"},
        {"type": "say", "who": "Chef", "what": "Listen up!"},
        {"type": "label", "label": "week1_challenge1_hottub"},
    ]
    assert live._crossed_after[live._key("/tmp/renforge-crossed")][1] == 8

    again = live.advance_until("/tmp/renforge-crossed", max_steps=1, timeout=2, mode="dismiss")
    assert again["crossed"] == []


def test_advance_until_rejects_bad_budgets():
    result = run_advance_until(lambda _s: {}, lambda _i: {}, max_steps=0, timeout=1)

    assert result["ok"] is False
    assert result["stop"] == "invalid_arguments"


def test_apply_screenshot_rejects_invalid_payload():
    with pytest.raises(ValueError):
        apply_screenshot(_raw(screenshot_base64="not-base64"))


def test_custom_overlay_hub_and_input_remain_decisions():
    for role in ("button", "input"):
        snapshot = classify(_raw(
            overlay_screens=["world_map"], say_dismiss="dismiss_hard_pause",
            elements=[_element(screen="world_map", role=role)],
        ))
        assert snapshot["forward"] == "choose"
        assert len(snapshot["controls"]) == 1
        assert snapshot["chrome"] == []
        assert guard_dismiss(snapshot, 4) == "not_dismiss"


def test_event_occurrences_preserve_repeated_lines_and_deduplicate_frames():
    events = [
        {"seq": 1, "type": "say", "say_id": 1, "who": "A", "what": "Yes."},
        {"seq": 2, "type": "say", "say_id": 2, "who": "A", "what": "Yes."},
        {"seq": 3, "type": "say", "say_id": 3, "who": "B", "what": "Yes."},
    ]
    frames = [{"event_seq": 2, "say_id": 2, "dialogue": {"who": "A", "what": "Yes."}}]
    assert crossed_transcript(events, frames) == [
        {"type": "say", "who": "A", "what": "Yes."},
        {"type": "say", "who": "A", "what": "Yes."},
        {"type": "say", "who": "B", "what": "Yes."},
    ]


@pytest.mark.parametrize("number", [float("nan"), float("inf"), -float("inf"), True])
def test_action_validation_rejects_nonfinite_numbers_before_dispatch(number):
    from renforge.interaction import validate_act_payload

    assert validate_act_payload({"interaction": 4, "id": "map/scroll", "x": 1, "y": number}) == "value_required"


def test_identity_guards_include_session_and_rebuilt_controls():
    snapshot = classify(_raw(session_id="launch-a", observation_id="revision-a",
                             elements=[_element(widget_id="go")]))
    assert guard_act(snapshot, 4, "say/go", session_id="launch-a", observation_id="revision-a") is None
    assert guard_act(snapshot, 4, "say/go", session_id="launch-b") == "stale"
    assert guard_act(snapshot, 4, "say/go", observation_id="revision-b") == "stale"


def test_act_returns_next_observation_without_advancing_or_consuming_transcript(monkeypatch):
    from renforge.tools import live

    calls = []

    class Client:
        def act(self, **kwargs):
            calls.append(kwargs)
            return {"ok": True, "operation": "click"}

        def observe(self, **kwargs):
            return _raw(interaction=5, session_id="a", observation_id="new")

    client = Client()
    monkeypatch.setattr(live, "_client", lambda path: client)
    result = live.act("/tmp/action-flow", 4, "say/go", screenshot=False, session_id="a", observation_id="old")
    assert result["ok"] and result["observation_status"] == "ready"
    assert result["observation"]["interaction"] == 5
    assert calls[0]["session_id"] == "a" and calls[0]["observation_id"] == "old"
    assert "/tmp/action-flow" not in live._crossed_after


def test_act_reports_followup_failure_without_retrying_successful_input(monkeypatch):
    from renforge.tools import live
    from renforge.bridge.client import BridgeError

    actions = []

    class Client:
        def act(self, **kwargs):
            actions.append(kwargs)
            return {"ok": True, "operation": "click"}

        def observe(self, **kwargs):
            raise BridgeError("disconnected")

    monkeypatch.setattr(live, "_client", lambda path: Client())
    result = live.act("/tmp/action-flow", 4, "say/go")
    assert result["ok"] is True
    assert result["observation_status"] == "transport_error"
    assert result["observation"] is None
    assert len(actions) == 1


def test_act_can_condense_action_and_progression(monkeypatch):
    from renforge.tools import live

    class Client:
        def act(self, **kwargs):
            return {"ok": True}

    client = Client()
    monkeypatch.setattr(live, "_client", lambda path: client)
    calls = []

    def progress(project, active_client, **kwargs):
        calls.append((active_client, kwargs))
        return {"ok": True, "stop": "choose", "steps": 2,
                "observation": classify(_raw()), "observation_status": "ready", "crossed": []}

    monkeypatch.setattr(live, "_advance_with_client", progress)
    result = live.act("/tmp/action-flow", 4, "say/go", advance=True, screenshot=False)
    assert result["observation_status"] == "ready"
    assert result["progress"]["stop"] == "choose"
    assert result["progress"]["steps"] == 2
    assert calls[0][0] is client and calls[0][1]["screenshot"] is False


def test_rendered_frame_replaces_raw_callback_text_for_the_same_occurrence():
    events = [{"seq": 1, "type": "say", "say_id": 1, "who": None, "what": "Hello [name]."}]
    frames = [{"event_seq": 1, "say_id": 1, "dialogue": {"who": None, "what": "Hello Alex."}}]
    assert crossed_transcript(events, frames) == [{"type": "say", "who": None, "what": "Hello Alex."}]


def test_lost_action_reply_is_explicitly_uncertain(monkeypatch):
    from renforge.tools import live
    from renforge.bridge.client import BridgeProtocolError
    from renforge.util.frames import FRAME_TIMEOUT

    class Client:
        def act(self, **kwargs):
            raise BridgeProtocolError("timed out waiting for a frame", code=FRAME_TIMEOUT)

    monkeypatch.setattr(live, "_client", lambda path: Client())
    result = live.act("/tmp/lost-action", 4, "say/go")
    assert result["action_outcome"] == "unknown"
    assert result["retry_safe"] is False
    assert result["observation_status"] == "timeout"


def test_stale_action_returns_classified_fresh_observation(monkeypatch):
    from renforge.tools import live

    class Client:
        def act(self, **kwargs):
            return {"ok": False, "error": "stale", "observation": _raw(interaction=5)}

    monkeypatch.setattr(live, "_client", lambda path: Client())
    result = live.act("/tmp/stale-action", 4, "say/go", screenshot=False)
    assert result["action_outcome"] == "refused"
    assert result["observation"]["interaction"] == 5
    assert "controls" in result["observation"]
    assert result["observation_status"] == "ready"


def test_an_unstable_refusal_reports_its_current_observation_as_ready(monkeypatch):
    """A key pressed during a splash-screen pause is refused at once."""
    from renforge.tools import live

    class Client:
        def act(self, **kwargs):
            return {"ok": False, "error": "unstable",
                    "observation": _raw(stable=False, unstable_reason="pause", dialogue=None)}

    monkeypatch.setattr(live, "_client", lambda path: Client())
    result = live.act("/tmp/unstable-action", 4, key="escape", screenshot=False)
    assert result["action_outcome"] == "refused"
    assert result["observation_status"] == "ready"
    assert result["observation"]["unstable_reason"] == "pause"
    assert "observe again" in result["hint"]


def test_gameplay_png_is_bounded_and_hashes_returned_bytes():
    import io
    import random
    from PIL import Image
    from renforge.util.images import MCP_PNG_MAX_BYTES

    source = Image.frombytes("RGB", (1600, 900), random.Random(1).randbytes(1600 * 900 * 3))
    output = io.BytesIO()
    source.save(output, format="PNG")
    original = output.getvalue()
    raw, png = apply_screenshot(_raw(screenshot_base64=base64.b64encode(original).decode()))
    assert len(original) > 1024 * 1024
    assert len(png) <= MCP_PNG_MAX_BYTES
    assert raw["frame_hash"] == hashlib.sha256(png).hexdigest()
    assert raw["screenshot"]["original_width"] == 1600
    assert raw["screenshot"]["scaled"]
    assert raw["screenshot"]["width"] < 1600
    assert raw["frame_id"] == hashlib.sha256(original).hexdigest()
    assert raw["frame"]["width"] == raw["screenshot"]["width"]
    assert classify(raw)["frame"] == raw["frame"]


def test_settle_rejects_departing_stable_frame(monkeypatch):
    from renforge.progression import settle
    frames = iter([
        _raw(interaction=1, observation_id="old"),
        _raw(interaction=1, observation_id="old", stable=False),
        _raw(interaction=2, observation_id="new"),
        _raw(interaction=2, observation_id="new"),
    ])
    import time
    result, status, error = settle(lambda shot: next(frames), time.monotonic() + 1, screenshot=False)
    assert status == "ready" and error is None
    assert result["interaction"] == 2


def test_settle_accepts_a_screen_that_keeps_changing_within_one_interaction():
    """A music room: a timer rebuilds its buttons every frame."""
    from renforge.progression import settle
    import itertools
    import time
    counter = itertools.count()
    result, status, error = settle(
        lambda shot: _raw(interaction=3, observation_id="frame-%d" % next(counter)),
        time.monotonic() + 2, screenshot=True)
    assert status == "ready" and error is None
    assert result["interaction"] == 3
    # Unstable frames do not count toward the run.
    flicker = itertools.count()
    result, status, _error = settle(
        lambda shot: _raw(interaction=3, observation_id="f%d" % next(flicker), stable=False),
        time.monotonic() + 0.3, screenshot=False)
    assert status == "timeout" and result is None
    # Inside a pause when the budget ends: the pause is the current state.
    result, status, _error = settle(
        lambda shot: _raw(interaction=4, stable=False, unstable_reason="pause"),
        time.monotonic() + 0.2, screenshot=True)
    assert status == "ready" and result["unstable_reason"] == "pause"


def test_a_pause_that_outlasts_any_short_pause_is_clicked_past_once(monkeypatch):
    """A {w=1.0}{nw} line stayed a "pause" until act ran out of time."""
    import time
    from renforge import progression
    monkeypatch.setattr(progression, "STUCK_PAUSE_SECONDS", 0.05)
    nudges = []
    stuck = _raw(interaction=7, stable=False, unstable_reason="pause", say_dismiss="dismiss")

    class Client:
        def progression(self, *args, **kwargs):
            raise AssertionError("progression must not start from an unstable frame")

    progression.run_native_skip(Client(), lambda shot: stuck, max_steps=5,
                                deadline=time.monotonic() + 0.3, total_deadline=time.monotonic() + 0.4,
                                nudge=nudges.append)
    assert nudges == [7]
    # Without a way to click (no nudge), it only waits.
    progression.run_native_skip(Client(), lambda shot: stuck, max_steps=5,
                                deadline=time.monotonic() + 0.1, total_deadline=time.monotonic() + 0.2)


def test_dismiss_progression_stops_before_unclassified_input():
    raw = _raw(omitted={'unclassified': True, 'focus_truncated': False})
    result, dismisses = _run([raw])
    assert result['stop'] == 'unsupported'
    assert dismisses == []


@pytest.mark.parametrize('exit_code,expected', [(None, 'transport_error'), (0, 'exited'), (1, 'crash')])
def test_process_failure_preserves_last_observation_without_confirming_an_end(monkeypatch, exit_code, expected):
    from types import SimpleNamespace
    from renforge.tools import live
    from renforge.bridge.client import BridgeError
    from renforge.progression import failure_status
    last = classify(_raw(lifecycle={'phase': 'playing', 'ending': None}))
    process = SimpleNamespace(poll=lambda: exit_code)

    class Client:
        _config = SimpleNamespace(host='127.0.0.1', port=1, token='process-test')
        def observe(self, **kwargs):
            raise BridgeError('disconnected')
        def poll_events(self, cursor, **kwargs):
            return {'ok': True, 'events': [], 'cursor': cursor}

    client = Client()
    monkeypatch.setitem(live._SESSIONS, live._key('/tmp/process-ending'), SimpleNamespace(client=client, process=process))
    monkeypatch.setattr(live, 'run_native_skip', lambda *args, **kwargs: {
        'ok': False, 'stop': 'transport_error', 'steps': 1, 'observation': last})
    result = live._advance_with_client('/tmp/process-ending', client, max_steps=10, timeout=1, screenshot=False)
    assert failure_status(BridgeError('disconnected'), process) == expected
    assert result['observation_status'] == expected
    assert result['stop'] == expected
    assert result['ending'] is None
    assert result['last_observation'] == last
    if exit_code is not None:
        assert result['process']['exit_code'] == exit_code


def test_a_poll_that_fails_once_does_not_truncate_the_transcript(monkeypatch):
    """A busy main thread timed out one poll; the next resumed from the cursor."""
    from types import SimpleNamespace
    from renforge.tools import live
    from renforge.bridge.client import BridgeError
    polls = []

    class Client:
        _config = SimpleNamespace(host='127.0.0.1', port=1, token='poll-test')
        def observe(self, **kwargs):
            return _raw(interaction=3, observation_id='same')
        def poll_events(self, cursor, **kwargs):
            polls.append(cursor)
            if len(polls) == 1:
                raise BridgeError('timed out waiting for a frame')
            return {'ok': True, 'events': [], 'cursor': cursor + 1}

    client = Client()
    monkeypatch.setitem(live._SESSIONS, live._key('/tmp/poll-retry'), SimpleNamespace(client=client, process=None))

    def skip(_client, _fetch, *, between, **_kwargs):
        between()
        between()
        return {'ok': True, 'stop': 'choose', 'steps': 2, 'observation': None}

    monkeypatch.setattr(live, 'run_native_skip', skip)
    result = live._advance_with_client('/tmp/poll-retry', client, max_steps=10, timeout=2, screenshot=False)
    assert result['crossed_truncated'] is False
    assert polls[1] == polls[0]  # the retry asked from the same cursor


def test_observation_id_alone_guards_act():
    from renforge.interaction import validate_act_payload

    snapshot = classify(_raw(observation_id="obs-1", session_id="s-1",
                             elements=[_element(screen="hub", text="Go", action="Jump('x')")]))
    control = snapshot["controls"][0]["id"]

    assert validate_act_payload({"id": control, "observation_id": "obs-1"}) is None
    assert validate_act_payload({"id": control}) == "stale"
    assert guard_act(snapshot, None, control, observation_id="obs-1") is None
    assert guard_act(snapshot, None, control, observation_id="obs-0") == "stale"
    assert guard_act(snapshot, None, control) == "stale"
    # The legacy interaction guard still works on its own.
    assert guard_act(snapshot, 4, control) is None
    assert guard_act(snapshot, 3, control, observation_id="obs-1") == "stale"


def test_seen_marks_repeated_statements_and_lines():
    assert classify(_raw(seen=True))["seen"] is True
    assert classify(_raw(seen=False))["seen"] is False
    assert classify(_raw())["seen"] is None

    events = [
        {"seq": 1, "type": "say", "who": "Elder", "what": "Attack?", "say_id": 1, "seen": False},
        {"seq": 2, "type": "say", "who": "Elder", "what": "You fell.", "say_id": 2, "seen": False},
        {"seq": 3, "type": "say", "who": "Elder", "what": "Attack?", "say_id": 3, "seen": True},
    ]
    assert crossed_transcript(events, []) == [
        {"type": "say", "who": "Elder", "what": "Attack?"},
        {"type": "say", "who": "Elder", "what": "You fell."},
        {"type": "say", "who": "Elder", "what": "Attack?", "seen": True},
    ]


def test_hud_buttons_kept_up_from_earlier_lines_do_not_hold_a_clickable_line():
    hud = [_element(screen="phone_button", action="Show('Phone')", text="Phone")]
    screens = [{"name": "say", "layer": "screens", "modal": False},
               {"name": "phone_button", "layer": "screens", "modal": False, "kept_up": True}]
    line = classify(_raw(elements=hud, screens=screens))
    assert line["forward"] == "dismiss"
    assert [c["id"] for c in line["controls"]] == ["phone_button/Show('Phone')"]  # still usable

    # Screens of this moment, a hard pause, or no line: a choice.
    called = [dict(s, kept_up=False) for s in screens]
    assert classify(_raw(elements=hud, screens=called))["forward"] == "choose"
    assert classify(_raw(elements=hud, screens=screens, say_dismiss="dismiss_hard_pause"))["forward"] == "choose"
    assert classify(_raw(elements=hud, screens=screens, dialogue=None))["forward"] == "choose"


def test_a_first_pause_with_only_hud_buttons_is_clicked_past():
    # `show text "Title Card"` then `pause`, under a kept-up HUD button.
    hud = [_element(screen="character_menu_button", action="ShowMenu('character_info_menu')")]
    screens = [{"name": "character_menu_button", "layer": "screens", "modal": False, "kept_up": True}]
    card = classify(_raw(elements=hud, screens=screens, dialogue=None, seen=False))
    assert card["forward"] == "dismiss"
    assert [c["screen"] for c in card["controls"]] == ["character_menu_button"]  # still usable
    # A pause the game already came back to is a hub waiting on those buttons.
    assert classify(_raw(elements=hud, screens=screens, dialogue=None, seen=True))["forward"] == "choose"
    assert classify(_raw(elements=hud, screens=screens, dialogue=None))["forward"] == "choose"


def test_interface_buttons_are_the_choice_when_nothing_else_moves_the_game():
    """A `call screen` disclaimer whose buttons are MainMenu() and Quit()."""
    buttons = [_element(screen="disclaimer", action="MainMenu", chrome=True),
               _element(screen="disclaimer", action="Quit", chrome=True)]
    screens = [{"name": "disclaimer", "layer": "screens", "modal": False}]
    snapshot = classify(_raw(elements=buttons, screens=screens, dialogue=None, say_dismiss=None))
    assert snapshot["forward"] == "choose"
    assert [c["action"] for c in snapshot["controls"]] == ["MainMenu", "Quit"]
    assert snapshot["chrome"] == []
    # Beside a line, or on a kept-up HUD or the game menu, they stay chrome.
    line = classify(_raw(elements=buttons, screens=screens))
    assert line["controls"] == [] and len(line["chrome"]) == 2
    hud = [dict(screens[0], kept_up=True)]
    assert classify(_raw(elements=buttons, screens=hud, dialogue=None, say_dismiss=None))["controls"] == []
    menu = [_element(screen="save", action="Quit", chrome=True)]
    saved = classify(_raw(elements=menu, screens=[{"name": "save", "layer": "screens", "modal": False}],
                          dialogue=None, say_dismiss=None))
    assert saved["controls"] == [] and saved["forward"] == "none"


def test_an_exception_screen_is_reported_as_a_game_error():
    """A game exception showed as an ordinary screen to choose from."""
    from renforge.interaction import progression_boundary
    error = {"message": "NameError: name 'visitedGhost' is not defined", "traceback_file": "traceback.txt"}
    buttons = [_element(screen="_exception", action="Partial", text="Ignore"),
               _element(screen="_exception", action="ErrorQuit", text="Quit")]
    screens = [{"name": "_exception", "layer": "screens", "modal": False}]
    snapshot = classify(_raw(elements=buttons, screens=screens, dialogue=None, say_dismiss=None, game_error=error))
    assert snapshot["game_error"] == error
    assert progression_boundary(snapshot) == "error"
    assert classify(_raw())["game_error"] is None


def test_a_scrolling_panel_beside_a_line_does_not_hold_it():
    # A livestream chat: a viewport and its scrollbar, re-shown with the line.
    chat = [_element(screen="stream", role="viewport", scroll=True),
            _element(screen="stream", role="bar", text="viewport vertical scroll", scroll=True)]
    screens = [{"name": "say", "layer": "screens", "modal": False},
               {"name": "stream", "layer": "screens", "modal": False}]
    line = classify(_raw(elements=chat, screens=screens))
    assert line["forward"] == "dismiss"
    assert all(c["scroll"] for c in line["controls"])  # still there to scroll
    # A slider bound to a value is input, so it still stops the line.
    slider = [_element(screen="stream", role="bar", text="volume")]
    assert classify(_raw(elements=slider, screens=screens))["forward"] == "choose"


def test_dismiss_past_optional_controls_still_refuses_menus_and_modals():
    hotspot = [_element(screen="shrine", role="hotspot", widget_id="touch")]
    line = classify(_raw(elements=hotspot))
    assert guard_dismiss(line, 4) == "not_dismiss"
    assert guard_dismiss(line, 4, past_optional_controls=True) is None
    modal = classify(_raw(elements=hotspot, screens=[{"name": "shrine", "layer": "screens", "modal": True}]))
    assert guard_dismiss(modal, 4, past_optional_controls=True) == "not_dismiss"
    menu = classify(_raw(say_dismiss=None, elements=[_element(screen="choice", menu_index=0, text="Go")]))
    assert guard_dismiss(menu, 4, past_optional_controls=True) == "not_dismiss"


def test_a_bar_that_only_shows_a_value_does_not_hold_a_line():
    """Health bars: ``bar value hp range max_hp`` is not adjustable."""
    snapshot = classify(
        _raw(
            screens=[{"name": "hp_bars", "layer": "screens", "modal": False}],
            elements=[_element(screen="hp_bars", role="bar", action=None, operations=[],
                               adjustment={"value": 80, "range": 100, "step": 1, "page": 10})],
        )
    )
    assert snapshot["forward"] == "dismiss"
    assert snapshot["controls"][0]["operations"] == []


def test_image_names_keep_their_tag():
    from renforge.interaction import image_basename
    assert image_basename(("lorbook", "naughtydn")) == "lorbook naughtydn"
    assert image_basename("lorbook naughtydn") == "lorbook naughtydn"
    assert image_basename("images/ui/hearticon.webp") == "hearticon.webp"


@pytest.mark.parametrize(
    ("say_dismiss", "expected"),
    [
        ("dismiss", "The line waits under the modal screen stats_page: close it"),
        (None, "Nothing to continue: the game is waiting for one of its controls."),
    ],
)
def test_continue_names_a_modal_screen_holding_a_line(monkeypatch, say_dismiss, expected):
    from renforge.tools import live

    observed = {
        "observation_id": "o1",
        "interaction": 3,
        "forward": "choose",
        "say_dismiss": say_dismiss,
        "dialogue": {"who": "Sis", "what": "Forgot something?"},
        "screens": [{"name": "say"}, {"name": "stats_page", "modal": True}],
    }
    monkeypatch.setattr(live, "observe", lambda *_a, **_k: dict(observed))
    # The bridge refuses to click past a line a modal screen holds.
    monkeypatch.setattr(live, "_with_client", lambda *_a, **_k: {"ok": False, "error": "modal"})
    monkeypatch.setattr(live, "advance_until", lambda *_a, **_k: {
        "ok": True, "stop": "choose", "steps": 0, "observation": dict(observed),
        "observation_status": "ready"})

    result = live.play("/tmp/renforge-modal-line", "o1", timeout=0.2)

    assert result["hint"].startswith(expected)


def test_a_pause_beside_buttons_that_do_nothing_is_dismissed():
    elements = [_element(screen="tutorial", role="imagebutton", text=None, inert=True,
                         image_name="book_locked.png", operations=["click"])]
    screens = [{"name": "tutorial", "layer": "screens", "modal": False}]
    snapshot = classify(_raw(dialogue=None, screens=screens, elements=elements))
    assert snapshot["forward"] == "dismiss"
    assert snapshot["controls"][0]["inert"] is True


def test_a_reply_that_times_out_while_the_game_is_busy_is_asked_again():
    """Entering a scene can hold the main thread past one reply's deadline."""
    import time

    from renforge.progression import run_native_skip, settle

    busy = {"ok": False, "status": "timeout", "error": "timed out waiting for a frame"}
    frames = iter([busy, _raw(interaction=2, observation_id="new"), _raw(interaction=2, observation_id="new")])
    result, status, error = settle(lambda shot: next(frames), time.monotonic() + 2, screenshot=False)
    assert status == "ready" and error is None and result["interaction"] == 2

    # Progression polls again too, and a menu it then sees is its stop.
    menu = _raw(interaction=3, observation_id="menu", say_dismiss=None, dialogue=None,
                screens=[{"name": "choice", "layer": "screens", "modal": True}],
                elements=[_element(screen="choice", text="Go", menu_index=0)])
    replies = iter([busy, menu])
    progress = run_native_skip(None, lambda shot: next(replies), max_steps=5,
                               deadline=time.monotonic() + 2, total_deadline=time.monotonic() + 2)
    assert progress["ok"] is True and progress["stop"] != "timeout"

    # Out of budget, a timeout is still reported.
    stuck = iter([busy] * 100)
    result, status, error = settle(lambda shot: next(stuck), time.monotonic() + 0.01, screenshot=False)
    assert status == "timeout"


def test_a_screens_key_bindings_are_reported_with_the_key_to_send():
    """A settings screen continues only on `key "K_SPACE" action Return()`."""
    from renforge.compact import compact_observation

    keys = [{"screen": "intro_prefs", "binding": "K_SPACE", "act": "key/space", "action": "Return()"}]
    snapshot = classify(_raw(dialogue=None, say_dismiss=None, keys=keys,
                             screens=[{"name": "intro_prefs", "layer": "screens", "modal": True}]))
    assert snapshot["keys"] == keys
    assert compact_observation(snapshot)["keys"] == keys
    assert "keys" not in compact_observation(classify(_raw()))


def test_act_on_an_id_the_screen_does_not_list_says_so(monkeypatch):
    from renforge.tools import live

    current = {"ok": True, "observation_id": "obs_2", "controls": [{"id": "choice/item/0"}]}
    monkeypatch.setattr(live, "act", lambda *_a, **_k: {"ok": False, "error": "missing",
                                                        "action_outcome": "refused"})
    monkeypatch.setattr(live, "observe", lambda *_a, **_k: dict(current))
    result = live.play("/game", "obs_1", "choice/item/7", screenshot=False)
    assert result["error"] == "missing"
    assert "'choice/item/7' is not a control" in result["message"]
    assert result["observation"]["observation_id"] == "obs_2"
