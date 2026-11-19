"""Observe-side fixes in bridge.rpy, run against the fake runtime of test_bridge_runtime.

Each test pins one behaviour: who a line belongs to, when the last line still
counts as the current dialogue, and how controls are classified.
"""

from __future__ import annotations

import sys
import types

from test_bridge_runtime import running_bridge  # noqa: F401 - shared bridge fixture


class _Character:
    """A Character as the bridge reads it: a name, maybe a dynamic one."""

    def __init__(self, name, dynamic=False):
        self.name = name
        self.dynamic = dynamic

    def __call__(self, *args, **kwargs):
        raise AssertionError("the bridge called the game's Character")


def _bridge():
    return sys.modules["_renforge_runtime"].bridge


# --- speaker lookup ----------------------------------------------------------


def test_speaker_defined_in_the_character_namespace(running_bridge):
    """`define character.e = Character(...)` lives in store.character, not the store."""
    renpy = running_bridge.renpy
    renpy.python = types.SimpleNamespace(store_dicts={
        "store.character": {"e": _Character("Eileen")},
        "store": vars(renpy.store),
    })
    lookup = running_bridge.globs["_renforge_character_display_name"]

    assert lookup("e") == "Eileen"


def test_character_namespace_wins_over_a_store_variable_of_the_same_name(running_bridge):
    renpy = running_bridge.renpy
    renpy.store.e = "a game variable"
    renpy.python = types.SimpleNamespace(store_dicts={
        "store.character": {"e": _Character("Eileen")},
        "store": vars(renpy.store),
    })

    assert running_bridge.globs["_renforge_character_display_name"]("e") == "Eileen"


def test_quoted_string_speaker_is_its_own_name(running_bridge):
    """`"Mysterious voice" "Hello"` stores the quoted source as the who expression."""
    lookup = running_bridge.globs["_renforge_character_display_name"]

    assert lookup('"Mysterious voice"') == "Mysterious voice"
    assert lookup("'Guard'") == "Guard"


def test_dynamic_character_name_is_read_from_the_store(running_bridge):
    renpy = running_bridge.renpy
    renpy.store.player_name = "Rin"
    renpy.store.mc = types.SimpleNamespace(nickname="Ri")
    renpy.store.hero = _Character("player_name", dynamic=True)
    renpy.store.buddy = _Character("mc.nickname", dynamic=True)
    lookup = running_bridge.globs["_renforge_character_display_name"]

    assert lookup("hero") == "Rin"
    assert lookup("buddy") == "Ri"


def test_dynamic_name_the_bridge_cannot_read_is_not_reported_as_its_source(running_bridge):
    """Calls are never evaluated, and the expression text is not a name."""
    renpy = running_bridge.renpy
    calls = []
    renpy.store.make_name = lambda: calls.append(1) or "Called"
    renpy.store.hero = _Character("make_name()", dynamic=True)
    renpy.store.ghost = _Character(lambda: calls.append(1) or "Called", dynamic=True)
    lookup = running_bridge.globs["_renforge_character_display_name"]

    assert lookup("hero") is None
    assert lookup("ghost") is None
    assert calls == []


def test_say_event_carries_a_character_namespace_speaker(running_bridge):
    renpy = running_bridge.renpy
    renpy.python = types.SimpleNamespace(store_dicts={
        "store.character": {"e": _Character("Eileen")},
        "store": vars(renpy.store),
    })
    renpy.store._last_say_who = "e"
    on_say = renpy.config.all_character_callbacks[-1]

    on_say("begin", what="Hello.")

    bridge = _bridge()
    assert bridge.last_who == "Eileen"
    says = [event for event in bridge.events if event["type"] == "say"]
    assert says[-1]["who"] == "Eileen"
