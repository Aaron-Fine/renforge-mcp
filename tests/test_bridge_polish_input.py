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
