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


# --- dialogue at a soft pause ---------------------------------------------------


class SayBehavior:
    """A soft `pause`: renpy.ui.saybehavior() with the default dismiss list."""

    def __init__(self):
        self.dismiss = ["dismiss"]


def _soft_pause_without_say_screen(renpy):
    renpy.display.focus.focus_list[:] = []
    renpy.game = types.SimpleNamespace(
        context=lambda: types.SimpleNamespace(
            interacting=True,
            scene_lists=types.SimpleNamespace(
                layers={"transient": [types.SimpleNamespace(displayable=SayBehavior())]}
            ),
        )
    )
    renpy.display.interface.ongoing_transition = {}
    renpy.get_filename_line = lambda: ("game/script.rpy", 20)
    renpy.config.overlay_screens = []
    renpy.get_screen = lambda name: None


def test_soft_pause_after_a_line_reports_no_dialogue(running_bridge):
    """A line, then `pause`: the ended line is not the pause's dialogue."""
    renpy = running_bridge.renpy
    _soft_pause_without_say_screen(renpy)
    renpy.store._last_say_who = None
    on_say = renpy.config.all_character_callbacks[-1]
    start_interact = renpy.config.start_interact_callbacks[0]

    on_say("begin", what="Hello.")
    on_say("show", what="Hello.")
    start_interact()
    # While the line's own interaction runs, its text is the dialogue even
    # when no say screen carries it (a custom say screen without `who`).
    during = running_bridge.client.observe(screenshot=False)
    assert during["dialogue"] == {"who": None, "what": "Hello."}

    on_say("end", what="Hello.")
    start_interact()
    paused = running_bridge.client.observe(screenshot=False)
    assert paused["say_dismiss"] == "dismiss"
    assert paused["dialogue"] is None


def test_line_left_by_an_exception_is_not_the_next_dialogue(running_bridge):
    """Rollback out of a line skips its "end" callback; interact_done still runs."""
    renpy = running_bridge.renpy
    _soft_pause_without_say_screen(renpy)
    renpy.store._last_say_who = None
    on_say = renpy.config.all_character_callbacks[-1]
    start_interact = renpy.config.start_interact_callbacks[0]

    on_say("begin", what="Hello.")
    on_say("show", what="Hello.")
    start_interact()
    on_say("interact_done", what="Hello.", exception=True)
    start_interact()

    assert running_bridge.client.observe(screenshot=False)["dialogue"] is None


def test_menu_caption_shown_without_interaction_is_the_menu_dialogue(running_bridge):
    """A caption said with interact=False stays up through the menu's interaction."""
    renpy = running_bridge.renpy
    _soft_pause_without_say_screen(renpy)
    renpy.store._last_say_who = None
    on_say = renpy.config.all_character_callbacks[-1]
    start_interact = renpy.config.start_interact_callbacks[0]

    for event in ("begin", "show", "show_done", "end"):
        on_say(event, what="Which way?", interact=False)
    start_interact()
    assert running_bridge.client.observe(screenshot=False)["dialogue"] == {"who": None, "what": "Which way?"}

    start_interact()
    assert running_bridge.client.observe(screenshot=False)["dialogue"] is None


# --- ending lifecycle -------------------------------------------------------


def _script_return_to_main_menu(renpy, monkeypatch):
    context = types.SimpleNamespace(_main_menu=False, _menu=False)
    monkeypatch.setattr(renpy, "context", lambda: context, raising=False)
    monkeypatch.setattr(renpy, "context_nesting_level", lambda: 0, raising=False)
    monkeypatch.setattr(renpy, "get_return_stack", lambda: [], raising=False)
    monkeypatch.setattr(renpy, "get_filename_line", lambda: ("game/story.rpy", 42), raising=False)
    for callback in renpy.config.statement_callbacks:
        callback("return")
    context._main_menu = True
    return context


def _ending_events(bridge):
    return [event for event in bridge.events if event["type"] == "ending"]


def test_ending_event_fires_at_the_main_menu_without_an_observe(running_bridge, monkeypatch):
    renpy = running_bridge.renpy
    _script_return_to_main_menu(renpy, monkeypatch)

    renpy.config.start_interact_callbacks[0]()

    bridge = _bridge()
    assert [event["reason"] for event in _ending_events(bridge)] == ["script_return"]
    assert running_bridge.globs["_renforge_game_lifecycle"]() == {"phase": "ended", "ending": {
        "id": 1, "reason": "script_return", "statement": {"file": "game/story.rpy", "line": 42}}}
    # A second menu interaction does not confirm it again.
    renpy.config.start_interact_callbacks[0]()
    assert len(_ending_events(bridge)) == 1


def test_ending_survives_starting_a_new_game_before_any_observe(running_bridge, monkeypatch):
    renpy = running_bridge.renpy
    _script_return_to_main_menu(renpy, monkeypatch)
    renpy.config.start_interact_callbacks[0]()
    for callback in renpy.config.label_callbacks:
        callback("start", False)

    assert len(_ending_events(_bridge())) == 1


def test_lifecycle_read_changes_nothing(running_bridge, monkeypatch):
    """observe is read-only: reading the lifecycle neither confirms nor emits."""
    renpy = running_bridge.renpy
    _script_return_to_main_menu(renpy, monkeypatch)
    bridge = _bridge()
    pending = dict(bridge.pending_ending)

    running_bridge.globs["_renforge_game_lifecycle"]()

    assert bridge.pending_ending == pending
    assert bridge.ending is None and bridge.ending_counter == 0
    assert _ending_events(bridge) == []


# --- kept_up screens ----------------------------------------------------------


class ScreenDisplayable:
    """A shown screen: a HUD is not transient, a say screen is."""

    def __init__(self, name, transient):
        self.screen_name = (name, None)
        self.transient = transient
        self.layer = "screens"
        self.modal = False
        self.hiding = False
        self.scope = {}
        self.child = None
        self.children = []


def _show_screens(renpy, *screens):
    renpy.display.focus.focus_list[:] = []
    renpy.game = types.SimpleNamespace(
        context=lambda: types.SimpleNamespace(
            interacting=True,
            scene_lists=types.SimpleNamespace(
                layers={"screens": [types.SimpleNamespace(displayable=screen) for screen in screens]}
            ),
        )
    )
    renpy.display.interface.ongoing_transition = {}
    renpy.get_filename_line = lambda: ("game/script.rpy", 30)
    renpy.config.overlay_screens = []
    renpy.get_screen = lambda name: None


def _kept_up(running_bridge):
    reply = running_bridge.client.observe(screenshot=False)
    return {screen["name"]: screen["kept_up"] for screen in reply["screens"]}


def test_hud_shown_during_an_unobserved_interaction_is_kept_up(running_bridge):
    """`show screen hud` then `pause`, unobserved; the next line keeps the HUD up."""
    renpy = running_bridge.renpy
    start_interact = renpy.config.start_interact_callbacks[0]
    hud = ScreenDisplayable("hud", False)

    _show_screens(renpy, hud)
    start_interact()  # the pause nobody observed
    _show_screens(renpy, hud, ScreenDisplayable("say", True))
    start_interact()  # the next line

    assert _kept_up(running_bridge) == {"hud": True, "say": False}


def test_screen_shown_with_this_interaction_is_not_kept_up(running_bridge):
    renpy = running_bridge.renpy
    _show_screens(renpy, ScreenDisplayable("hud", False))
    renpy.config.start_interact_callbacks[0]()

    assert _kept_up(running_bridge) == {"hud": False}


def test_first_seen_forgets_screens_that_left_and_checks_the_name(running_bridge):
    """A stamp belongs to one shown screen; a reused object id does not inherit it."""
    renpy = running_bridge.renpy
    start_interact = renpy.config.start_interact_callbacks[0]
    hud = ScreenDisplayable("hud", False)
    _show_screens(renpy, hud)
    start_interact()
    _show_screens(renpy)
    start_interact()

    assert not _bridge().screen_first_seen

    # Same object id, another screen: as Python may reuse a freed screen's id.
    _show_screens(renpy, hud)
    start_interact()
    hud.screen_name = ("inventory", None)
    start_interact()
    assert _kept_up(running_bridge) == {"inventory": False}
