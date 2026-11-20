"""Follow-up fixes in bridge.rpy, run against the fake runtime of bridge_harness.

These pin the bridge halves of fixes whose Python halves landed elsewhere: the
stuck-pause dismiss, the shared custom-action rule, one scene inventory per
semantic click, and engine widget subclasses in the kinetic classifier.
"""

from __future__ import annotations

import sys
import types

import pytest
from bridge_harness import _arm_stable_interaction, _completed_render


def _bridge():
    return sys.modules["_renforge_runtime"].bridge


# --- dismiss_if: the stuck-pause opt-in ---------------------------------------


def _pausing_line(running_bridge, monkeypatch, *, reason="pause", say_dismiss="dismiss"):
    """Serve a say line whose frame is unstable only because ``reason`` is pending."""
    bridge = _bridge()
    raw = {"ok": True, "interaction": bridge.observe_generation, "stable": False,
           "unstable_reason": reason, "session_id": bridge.session_id, "observation_id": "frame",
           "screens": [], "elements": [], "say_dismiss": say_dismiss,
           "dialogue": {"who": None, "what": "Wait for it."}}
    monkeypatch.setitem(running_bridge.globs, "_renforge_observe_raw", lambda *a, **k: dict(raw))
    running_bridge.renpy._queued_events[:] = []
    return raw


def test_dismiss_if_clicks_a_pending_pause_only_when_asked(running_bridge, monkeypatch):
    """A `{w=1.0}{nw}` line can stay a pause; the nudge's opt-in clicks it once."""
    raw = _pausing_line(running_bridge, monkeypatch)
    client = running_bridge.client

    refused = client.dismiss_if(interaction=raw["interaction"])
    assert refused == {"ok": False, "error": "unstable", "interaction": raw["interaction"]}
    assert running_bridge.renpy._queued_events == []

    posted = client.dismiss_if(interaction=raw["interaction"], allow_pause=True)
    assert posted.get("ok") is True, posted
    assert running_bridge.renpy._queued_events == ["dismiss"]


def test_dismiss_if_pause_opt_in_must_be_true_itself(running_bridge, monkeypatch):
    """Only a literal ``true`` opts in, as with ``past_optional_controls``."""
    raw = _pausing_line(running_bridge, monkeypatch)
    reply = running_bridge.client.request("dismiss_if", {"interaction": raw["interaction"], "allow_pause": "yes"})
    assert reply.get("ok") is False and reply.get("error") == "unstable", reply
    assert running_bridge.renpy._queued_events == []


@pytest.mark.parametrize(("reason", "say_dismiss", "error"), [
    ("transition", "dismiss", "unstable"),  # only a pause is let through
    ("pause", None, "not_dismiss"),  # a hard pause takes no click
])
def test_dismiss_if_pause_opt_in_keeps_every_other_check(running_bridge, monkeypatch, reason, say_dismiss, error):
    raw = _pausing_line(running_bridge, monkeypatch, reason=reason, say_dismiss=say_dismiss)
    reply = running_bridge.client.dismiss_if(interaction=raw["interaction"], allow_pause=True)
    assert reply.get("ok") is False and reply.get("error") == error, reply
    assert running_bridge.renpy._queued_events == []


# --- custom click attributes: one rule for discovery and the click ------------


def _custom_widget_scene(running_bridge, widgets):
    """Draw ``widgets`` side by side on one settled screen."""
    renpy = running_bridge.renpy

    class ScreenDisplayable:
        screen_name = ("hotspots", None)
        layer = "screens"
        modal = False
        hiding = False
        widgets = {}
        scope = {}

    screen = ScreenDisplayable()
    screen.children = list(widgets)
    renpy.display.behavior.is_sensitive = lambda action: True
    _arm_stable_interaction(running_bridge, generation=9)
    renpy.game.context = lambda: types.SimpleNamespace(interacting=True,
        scene_lists=types.SimpleNamespace(layers={"screens": [types.SimpleNamespace(displayable=screen)]}))
    renpy.config.screen_width, renpy.config.screen_height = 800, 600
    renpy.display.render = types.SimpleNamespace(screen_render=_completed_render(800, 600, [screen],
        children=[(_completed_render(60, 40, [widget]), 100 * index, 100, True, True)
                  for index, widget in enumerate(widgets)]))


def test_custom_click_attributes_are_judged_alike_by_discovery_and_the_click(running_bridge, monkeypatch):
    """Actions make an enabled control, nothing a disabled one, a counter none."""
    from renforge.interaction import classify

    renpy = running_bridge.renpy

    class Action:
        def __call__(self):
            return None

    class Hotspot:
        def __init__(self, **attrs):
            vars(self).update(attrs)

        def event(self, ev, x, y, st):
            raise AssertionError("inspection must not invoke custom input")

    acting, empty, counter = Hotspot(click_action=[Action()]), Hotspot(action=None), Hotspot(click_count=0)
    monkeypatch.setattr(renpy, "ui", types.SimpleNamespace(Action=Action), raising=False)
    _custom_widget_scene(running_bridge, [acting, empty, counter])

    controls = classify(running_bridge.client.observe(screenshot=False))["controls"]
    listed = {control["bounds"]["x"]: control["enabled"] for control in controls}
    assert listed == {0: True, 100: False}

    value = running_bridge.globs["_renforge_custom_action_value"]
    assert value(acting) == acting.click_action
    assert value(empty) is None and value(counter) is None


# --- a semantic click walks the scene once -----------------------------------


def _acting_hotspot(running_bridge, monkeypatch):
    """One game displayable whose click attribute holds an Action; returns its runs."""
    renpy = running_bridge.renpy
    runs = []

    class Action:
        def __call__(self):
            runs.append(1)

    class Hotspot:
        def __init__(self):
            self.click_action = Action()

        def event(self, ev, x, y, st):
            raise AssertionError("a semantic click runs the action, not the event")

    monkeypatch.setattr(renpy, "ui", types.SimpleNamespace(Action=Action), raising=False)
    renpy.display.behavior.run = lambda action: action()
    _custom_widget_scene(running_bridge, [Hotspot()])
    return runs


def _count_inventories(running_bridge, monkeypatch):
    original = running_bridge.globs["_renforge_scene_inventory"]
    calls = []

    def counting():
        calls.append(1)
        return original()

    monkeypatch.setitem(running_bridge.globs, "_renforge_scene_inventory", counting)
    return calls


def test_a_semantic_click_builds_the_scene_inventory_once(running_bridge, monkeypatch):
    runs = _acting_hotspot(running_bridge, monkeypatch)
    element = running_bridge.client.list_ui_elements()[0]
    calls = _count_inventories(running_bridge, monkeypatch)

    clicked = running_bridge.client.click_element(id=element["id"])
    assert clicked.get("ok") is True and clicked.get("via") == "action", clicked
    assert runs == [1]
    assert len(calls) == 1


def test_a_semantic_click_on_an_unsettled_frame_is_refused_with_the_observation(running_bridge, monkeypatch):
    runs = _acting_hotspot(running_bridge, monkeypatch)
    element = running_bridge.client.list_ui_elements()[0]
    monkeypatch.setitem(running_bridge.globs, "_renforge_transition_pending", lambda: True)

    refused = running_bridge.client.click_element(id=element["id"])
    assert refused.get("ok") is False and refused.get("error") == "unstable", refused
    assert refused["observation"]["unstable_reason"] == "transition"
    assert runs == []


# --- kinetic controls: engine widget subclasses ------------------------------


def _engine_kinetic_classes(renpy, monkeypatch):
    """Stand-ins for Ren'Py's Bar, Viewport and Drag, with a subclass of each."""
    classes = {name: type(name, (), {}) for name in ("Bar", "Viewport", "Drag")}
    monkeypatch.setattr(renpy.display.behavior, "Bar", classes["Bar"], raising=False)
    monkeypatch.setattr(renpy.display, "viewport", types.SimpleNamespace(Viewport=classes["Viewport"]), raising=False)
    monkeypatch.setattr(renpy.display, "dragdrop", types.SimpleNamespace(Drag=classes["Drag"]), raising=False)
    return {
        "bar": type("VolumeSlider", (classes["Bar"],), {}),
        "viewport": type("VPGrid", (classes["Viewport"],), {}),
        "drag": type("Card", (classes["Drag"],), {}),
    }


def test_kinetic_kind_treats_engine_subclasses_like_their_base(running_bridge, monkeypatch):
    subclasses = _engine_kinetic_classes(running_bridge.renpy, monkeypatch)
    kind = running_bridge.globs["_renforge_kinetic_kind"]

    assert {name: kind(cls()) for name, cls in subclasses.items()} == {name: name for name in subclasses}
    # A game's own class is not a kinetic control because of its name alone,
    assert kind(type("Slider", (), {})()) is None
    # but a class named like the engine's still is where the engine class is unknown.
    assert kind(type("Viewport", (), {})()) == "viewport"
    assert kind(None) is None


def test_observe_lists_an_unfocused_engine_subclass_as_its_kind(running_bridge, monkeypatch):
    from renforge.interaction import classify

    subclasses = _engine_kinetic_classes(running_bridge.renpy, monkeypatch)
    grid = subclasses["viewport"]()
    _custom_widget_scene(running_bridge, [grid])

    raw = running_bridge.client.observe(screenshot=False)
    assert [element["role"] for element in raw["elements"]] == ["viewport"], raw["elements"]
    assert classify(raw)["omitted"]["unclassified"] is False


# --- save and load slots that are paths (the bridge half of SAVE_SLOT_INVALID) --


_PATH_SLOTS = ["../escape", "..", ".", " .. ", "sub/slot", "sub\\slot", "/abs/slot", "C:slot", "C:\\slot",
               "nul\x00slot",
               # A colon is Windows stream syntax: "page:stream" writes a stream of "page".
               "page:stream", "1-1:$DATA"]


@pytest.mark.parametrize("command", ["save_slot", "load_slot"])
@pytest.mark.parametrize("slot", _PATH_SLOTS)
def test_save_and_load_refuse_a_slot_that_is_a_path(running_bridge, command, slot):
    """Ren'Py joins the slot onto its save folder; the host refuses these first, and so does the bridge."""
    calls: list[str] = []
    running_bridge.renpy.can_save = lambda: True
    running_bridge.renpy.save = lambda name, extra_info="": calls.append("save")
    running_bridge.renpy.can_load = lambda name: calls.append("can_load") or True
    running_bridge.renpy.load = lambda name: calls.append("load")

    reply = running_bridge.client.request(command, {"slot": slot})
    assert reply.get("ok") is False and reply.get("code") == "SAVE_SLOT_INVALID", reply
    assert calls == []


@pytest.mark.parametrize("slot", ["1-1", "quick-1", "branch.a", "a..b"])
def test_save_accepts_a_plain_slot_name(running_bridge, slot):
    saved: list[str] = []
    running_bridge.renpy.can_save = lambda: True
    running_bridge.renpy.save = lambda name, extra_info="": saved.append(name)

    reply = running_bridge.client.request("save_slot", {"slot": slot})
    assert reply.get("ok") is True, reply
    assert saved == [slot]


def test_a_custom_click_runs_the_action_discovery_listed_not_a_clicked_flag(running_bridge, monkeypatch):
    """A game displayable may keep ``action`` and a ``clicked`` press flag."""
    renpy = running_bridge.renpy
    runs = []

    class Action:
        def __call__(self):
            runs.append(1)

    class Hotspot:
        def __init__(self):
            self.action = Action()
            self.clicked = False

        def event(self, ev, x, y, st):
            raise AssertionError("a semantic click runs the action, not the event")

    ran = []
    monkeypatch.setattr(renpy, "ui", types.SimpleNamespace(Action=Action), raising=False)
    renpy.display.behavior.run = lambda action: ran.append(action) or (action() if callable(action) else None)
    _custom_widget_scene(running_bridge, [Hotspot()])
    element = running_bridge.client.list_ui_elements()[0]

    clicked = running_bridge.client.click_element(id=element["id"])
    assert clicked.get("ok") is True and clicked.get("via") == "action", clicked
    assert runs == [1]
    assert all(isinstance(action, Action) for action in ran), ran


# --- hovering a semantic control needs a pointer that reaches it ------------


def _semantic_hotspot_under(running_bridge, monkeypatch, focus_at_point):
    """A custom semantic control at (0, 100, 60x40) and the engine hit test given."""
    _acting_hotspot(running_bridge, monkeypatch)
    running_bridge.renpy.display.render.screen_render.focus_at_point = focus_at_point
    running_bridge.renpy._moves[:] = []
    return running_bridge.client.list_ui_elements()[0]


def test_hovering_a_semantic_control_avoids_a_point_another_control_takes(running_bridge, monkeypatch):
    upper = object()
    element = _semantic_hotspot_under(running_bridge, monkeypatch,
        lambda x, y, _screen: (upper, None, None) if x < 40 else None)
    hovered = running_bridge.client.hover_element(id=element["id"])
    assert hovered.get("ok") is True, hovered
    assert hovered["x"] >= 40, hovered


def test_hovering_a_semantic_control_another_control_covers_is_refused(running_bridge, monkeypatch):
    upper = object()
    element = _semantic_hotspot_under(running_bridge, monkeypatch, lambda x, y, _screen: (upper, None, None))
    assert element["clickable"] is True  # its action can still run
    hovered = running_bridge.client.hover_element(id=element["id"])
    assert hovered.get("ok") is False and hovered["error"] == "UI element has no pointer target", hovered
    assert running_bridge.renpy._moves == []
