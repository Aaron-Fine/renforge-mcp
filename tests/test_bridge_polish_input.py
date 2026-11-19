"""Regression tests for bridge input, click, hover, and hit-test polish.

These drive the real ``bridge.rpy`` body through the shared ``running_bridge``
fixture (a fake ``renpy`` module and a live ``BridgeClient`` connection).
"""

from __future__ import annotations

import struct
import types

import pytest
from test_bridge_runtime import _FakeFocus, _FakeWidget, running_bridge  # noqa: F401 - shared bridge fixture


# --- load_slot ---------------------------------------------------------------


def _record_business(running_bridge, monkeypatch):
    """Record business events as emitted (an inline restart stops the drain)."""
    globs = running_bridge.globs
    original = globs["_renforge_emit_business"]
    emitted = []

    def record(event_name, **data):
        emitted.append((event_name, data))
        return original(event_name, **data)

    monkeypatch.setitem(globs, "_renforge_emit_business", record)
    return emitted


def test_a_deferred_load_reports_load_completed_once(running_bridge, monkeypatch):
    """From Ren'Py 8.2 the load runs later, from the engine's invoke queue.

    The handler already reported it; the queued load raising the engine's
    restart must not report a second, uncorrelated load.completed.
    """
    renpy = running_bridge.renpy
    RestartTopContext = type("RestartTopContext", (Exception,), {})
    renpy.game = types.SimpleNamespace(RestartTopContext=RestartTopContext)
    renpy.can_load = lambda slot: True

    def load(slot):
        raise RestartTopContext()

    renpy.load = load
    emitted = _record_business(running_bridge, monkeypatch)
    queued = []
    renpy.invoke_in_main_thread = lambda fn, *args, **kwargs: queued.append(fn)

    reply = running_bridge.client.load_slot("branch-a")
    assert reply["ok"] is True and "restarting" not in reply
    assert len(queued) == 1
    with pytest.raises(RestartTopContext) as raised:
        queued[0]()
    assert not hasattr(raised.value, "renforge_result")
    assert [name for name, _data in emitted] == ["load.completed"]


def test_an_inline_load_still_reports_the_restart_once(running_bridge, monkeypatch):
    """Before 8.2 the load runs inside the request and raises the restart."""
    renpy = running_bridge.renpy
    RestartTopContext = type("RestartTopContext", (Exception,), {})
    renpy.game = types.SimpleNamespace(RestartTopContext=RestartTopContext)
    renpy.can_load = lambda slot: True

    def load(slot):
        raise RestartTopContext()

    renpy.load = load
    emitted = _record_business(running_bridge, monkeypatch)
    del renpy.invoke_in_main_thread
    reply = running_bridge.client.load_slot("teams")
    assert reply["ok"] is True and reply["restarting"] is True
    assert emitted == [("load.completed", {"slot": "teams", "restored_label": None})]


# --- custom click attributes ----------------------------------------------


class _CountingMapButton:
    """A game's focusable displayable that counts clicks, holding no action."""

    def __init__(self):
        self.click_count = 0


class _FlagButton:
    def __init__(self):
        self.clickable = True


def test_a_custom_click_attribute_that_holds_no_action_is_not_run(running_bridge):
    globs = running_bridge.globs
    for widget in (_CountingMapButton(), _FlagButton()):
        focus = types.SimpleNamespace(widget=widget, screen=None)
        assert globs["_renforge_focus_action_value"](focus) is None
        # Nothing to run, so the caller clicks the pointer instead.
        assert globs["_renforge_invoke_focus_action"](focus) is False


def test_a_custom_click_attribute_must_hold_renpy_actions_when_they_exist(running_bridge):
    renpy = running_bridge.renpy
    globs = running_bridge.globs

    class Action:
        def __call__(self):
            return None

    class Hotspot:
        def __init__(self, click_action):
            self.click_action = click_action

    actions = [Action(), Action()]
    renpy.ui = types.SimpleNamespace(Action=Action)
    try:
        focus = types.SimpleNamespace(widget=Hotspot(actions), screen=None)
        assert globs["_renforge_focus_action_value"](focus) is actions
        # A plain function is not a Ren'Py Action, as discovery requires.
        focus = types.SimpleNamespace(widget=Hotspot(lambda: None), screen=None)
        assert globs["_renforge_focus_action_value"](focus) is None
    finally:
        del renpy.ui


# --- input length ------------------------------------------------------------


def test_input_length_zero_is_no_limit(running_bridge):
    """Input.event checks ``if self.length:``, so 0 means unlimited."""
    widget = types.SimpleNamespace(length=0, allow=None, exclude=None)
    assert running_bridge.globs["_renforge_input_filtered"](widget, "hello") == "hello"
    widget.length = 3
    assert running_bridge.globs["_renforge_input_filtered"](widget, "hello") == "hel"


# --- self-voicing label fallback -------------------------------------------


class _ImageOnlyButton:
    """An image button with no Text child: its label comes from _tts_all."""

    def __init__(self, label, prefix):
        self._label = label
        self.style = types.SimpleNamespace(prefix=prefix)

    def _tts_all(self, raw=False):
        return self._label


def test_selected_is_kept_in_the_alt_of_a_button_that_is_not_selected(running_bridge):
    focus_text = running_bridge.globs["_renforge_focus_text"]
    assert focus_text(_ImageOnlyButton("Delete selected", "idle_")) == "Delete selected"
    # Ren'Py appends " selected" only to a selected button's label.
    assert focus_text(_ImageOnlyButton("Delete selected", "selected_idle_")) == "Delete"


def test_selected_follows_is_selected_on_engines_without_a_style_prefix(running_bridge):
    focus_text = running_bridge.globs["_renforge_focus_text"]

    class OlderButton:
        def __init__(self, selected):
            self._selected = selected
            self.style = types.SimpleNamespace()

        def is_selected(self):
            return self._selected

        def _tts_all(self):
            return "Save selected"

    assert focus_text(OlderButton(False)) == "Save selected"
    assert focus_text(OlderButton(True)) == "Save"


# --- hit tests ---------------------------------------------------------------


def _one_button_elements(running_bridge, monkeypatch, hit):
    globs = running_bridge.globs
    focus = _FakeFocus("Go", 10, 10, 100, 20)
    element = {"id": "go", "text": "Go", "covered": None, "clickable": False,
               "bounds": {"x": 10, "y": 10, "width": 100, "height": 20}}
    monkeypatch.setitem(globs, "_renforge_focusable_elements", lambda *a, **k: [(focus, element)])
    monkeypatch.setitem(globs, "_renforge_render_hit", lambda x, y: hit)
    return element


def test_hit_test_reports_no_receiver_when_the_engine_hits_nothing(running_bridge, monkeypatch):
    """A transparent part of a masked button, or a gap no render focuses."""
    _one_button_elements(running_bridge, monkeypatch, "none")
    hit = running_bridge.client.hit_test(60, 20)
    assert hit["ok"] is True
    assert hit["topmost"] is None
    assert [element["id"] for element in hit["underneath"]] == ["go"]


def test_hit_test_falls_back_to_rectangles_without_a_render(running_bridge, monkeypatch):
    _one_button_elements(running_bridge, monkeypatch, "unavailable")
    hit = running_bridge.client.hit_test(60, 20)
    assert hit["topmost"]["id"] == "go"
    assert hit["underneath"] == []


# --- one scene inventory per request ---------------------------------------


def _count_inventories(running_bridge, monkeypatch):
    globs = running_bridge.globs
    original = globs["_renforge_scene_inventory"]
    calls = []

    def counting():
        calls.append(1)
        return original()

    monkeypatch.setitem(globs, "_renforge_scene_inventory", counting)
    return calls


def test_click_element_builds_the_scene_inventory_once(running_bridge, monkeypatch):
    element = running_bridge.client.list_ui_elements()[1]
    calls = _count_inventories(running_bridge, monkeypatch)
    clicked = running_bridge.client.click_element(id=element["id"])
    assert clicked["ok"] is True, clicked
    assert clicked["received_by"]["id"] == element["id"]
    assert len(calls) == 1


def test_get_ui_element_bounds_builds_the_scene_inventory_once(running_bridge, monkeypatch):
    from test_bridge_runtime import _completed_render

    renpy = running_bridge.renpy
    widget = renpy.display.focus.focus_list[-1].widget
    renpy.config.screen_width, renpy.config.screen_height = 800, 600
    renpy.display.render = types.SimpleNamespace(screen_render=_completed_render(800, 600,
        children=[(_completed_render(100, 30, [widget]), 200, 100, True, True)]))
    element = running_bridge.client.list_ui_elements()[-1]
    calls = _count_inventories(running_bridge, monkeypatch)
    reply = running_bridge.client.get_ui_element_bounds(id=element["id"])
    assert reply["painted_bounds_available"] is True, reply
    assert reply["painted_bounds"] == {"x": 204, "y": 106, "width": 80, "height": 14}
    assert len(calls) == 1


# --- hover uses the click's point ------------------------------------------


def test_hover_goes_to_the_point_a_click_would_use(running_bridge):
    """Half the control is behind another one: hover the proven visible half."""
    renpy = running_bridge.renpy
    target = _FakeWidget("Partly visible")
    upper = object()
    renpy.display.focus.focus_list[:] = [_FakeFocus("", 10, 10, 100, 20, widget=target)]
    renpy.display.render = types.SimpleNamespace(screen_render=types.SimpleNamespace(
        focus_at_point=lambda x, y, _screen: (target if x < 45 else upper, None, None)))
    element = running_bridge.client.list_ui_elements()[0]
    clicked_x = element["click_point"]["x"]
    assert clicked_x < 45
    hovered = running_bridge.client.hover_element(id=element["id"])
    assert hovered["ok"] is True, hovered
    assert (hovered["x"], hovered["y"]) == (clicked_x, element["click_point"]["y"])


def test_hover_refuses_a_control_with_no_reachable_point(running_bridge):
    renpy = running_bridge.renpy
    target = _FakeWidget("Hidden")
    upper = object()
    renpy.display.focus.focus_list[:] = [_FakeFocus("", 10, 10, 100, 20, widget=target)]
    renpy.display.render = types.SimpleNamespace(screen_render=types.SimpleNamespace(
        focus_at_point=lambda x, y, _screen: (upper, None, None)))
    element = running_bridge.client.list_ui_elements()[0]
    assert element["clickable"] is False
    hovered = running_bridge.client.hover_element(id=element["id"])
    assert hovered["ok"] is False
    assert hovered["error"] == "UI element has no eligible click"
    assert renpy._moves == []


# --- input replacement errors ---------------------------------------------


class _FailingInput:
    """Input whose InputValue callback raises, as Input.update_text runs it."""

    def __init__(self):
        self.content = "Zack"
        self.caret_pos = 4
        self.length = None
        self.allow = None
        self.exclude = None
        self.calls = 0

    def update_text(self, new_content, editable, check_size=False):
        self.calls += 1
        self.content = new_content
        raise ValueError("name is reserved")


def test_input_callback_errors_are_reported_not_swallowed(running_bridge):
    widget = _FailingInput()
    with pytest.raises(ValueError, match="reserved"):
        running_bridge.globs["_renforge_replace_input_text"](widget, "Alex")
    assert widget.calls == 1

    class Input(_FailingInput):
        """Named like Ren'Py's Input, which the focused-input check accepts."""

    running_bridge.renpy._focused_widget = Input()
    reply = running_bridge.client.request("send_input", {"text": "Alex"})
    assert reply.get("ok") is not True
    assert "ValueError: name is reserved" in str(reply.get("error"))


def test_input_value_errors_are_reported_not_swallowed(running_bridge):
    """A widget without update_text is replaced through its InputValue."""

    class Input:
        def __init__(self):
            self.content = "Zack"
            self.value = self

        def set_text(self, text):
            raise ValueError("name is reserved")

    running_bridge.renpy._focused_widget = Input()
    reply = running_bridge.client.request("send_input", {"text": "Alex"})
    assert reply.get("ok") is not True
    assert "ValueError: name is reserved" in str(reply.get("error"))


def test_input_update_text_without_check_size_is_still_supported(running_bridge):
    class TwoArgumentInput:
        def __init__(self):
            self.content = ""
            self.length = None
            self.allow = None
            self.exclude = None

        def update_text(self, new_content, editable):
            self.content = new_content

    widget = TwoArgumentInput()
    assert running_bridge.globs["_renforge_replace_input_text"](widget, "Alex") == "Alex"
    assert widget.content == "Alex"


# --- eval switch -------------------------------------------------------------


@pytest.mark.parametrize("value", ["OFF", "0", "false", "no", " off "])
def test_any_eval_setting_other_than_on_switches_eval_off(running_bridge, monkeypatch, value):
    monkeypatch.setenv("RENFORGE_BRIDGE_EVAL", value)
    reply = running_bridge.client.request("eval", {"expr": "1 + 1"})
    assert reply["error"] == "eval_disabled"
    assert running_bridge.client.session_info()["eval_enabled"] is False


@pytest.mark.parametrize("value", [None, "on", "ON"])
def test_eval_runs_when_unset_or_on(running_bridge, monkeypatch, value):
    if value is None:
        monkeypatch.delenv("RENFORGE_BRIDGE_EVAL", raising=False)
    else:
        monkeypatch.setenv("RENFORGE_BRIDGE_EVAL", value)
    assert running_bridge.client.eval_expr("1 + 1") == 2
