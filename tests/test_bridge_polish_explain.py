"""Regression tests for explain, progression and act fixes in the bridge.

Script and screen nodes are built from ``type(<Ren'Py class name>, (), {})``
so the bridge's class-name dispatch sees the names the engine produces.
"""

from __future__ import annotations

import time
import types

import pytest
from test_bridge_explain import _node, _screen, _script_node, _set_screens, _set_script
from test_bridge_runtime import _arm_stable_interaction, _FakeFocus, _native_skip_setup
from test_bridge_runtime import running_bridge  # noqa: F401 - shared bridge fixture


# --- values: raw conditions, not the shortened entry text ------------------


def _screen_with_if(condition, keyword=()):
    f = "game/map.rpy"
    block = _node("SLBlock", children=[], keyword=list(keyword))
    button = _script_node("SLDisplayable", f, 3, name="button", positional=[], keyword=[], location=(f, 3),
                          children=[_script_node("SLIf", f, 4, entries=[(condition, block)], location=(f, 4))])
    return _screen((f, 1), ast=_node("SLScreen", children=[button]))


def test_a_screen_if_that_sets_properties_still_reports_its_values(running_bridge):
    running_bridge.renpy.store.map_locked = False
    _set_screens(running_bridge, {("map", None): _screen_with_if(
        "not map_locked", keyword=[("action", "Function(go)")])})

    reply = running_bridge.client.explain(screen="map")

    assert reply["nodes"][1]["text"] == "not map_locked : action Function(go)"
    assert reply["values"] == {"map_locked": False}
    assert not [key for key in reply if key.startswith("_")]


def test_a_condition_longer_than_the_entry_text_still_reports_its_values(running_bridge):
    store = running_bridge.renpy.store
    store.points = 5
    flags = ["flag_number_%02d" % number for number in range(12)]
    for name in flags:
        setattr(store, name, True)
    condition = "points > 3 and " + " and ".join(flags)
    assert len(condition) > 160
    _set_screens(running_bridge, {("map", None): _screen_with_if(condition)})

    reply = running_bridge.client.explain(screen="map")

    assert reply["nodes"][1]["text"].endswith("...")
    assert reply["values"]["points"] == 5
    assert reply["values"]["flag_number_11"] is True


def test_a_screen_loop_variable_is_not_read_from_the_store(running_bridge):
    f = "game/list.rpy"
    store = running_bridge.renpy.store
    store.item, store.key, store.quota = "stale", "stale", 2
    check = _script_node("SLIf", f, 3, entries=[("item.count > quota and key", _node("SLBlock", children=[]))])
    loop = _script_node("SLFor", f, 2, variable="(key, item)", expression="stock.items()", children=[check])
    _set_screens(running_bridge, {("list", None): _screen((f, 1), ast=_node("SLScreen", children=[loop]))})

    assert running_bridge.client.explain(screen="list")["values"] == {"quota": 2}


def test_explain_source_on_a_script_if_reports_its_values(running_bridge):
    f = "game/a.rpy"
    running_bridge.renpy.store.points = 5
    say = _script_node("Say", f, 4, who=None, what="Enough points.")
    check = _script_node("If", f, 3, entries=[("points > 3", [say])])
    _set_script(running_bridge, {})
    running_bridge.renpy.game.script.all_stmts = [check]
    _set_screens(running_bridge, {})

    reply = running_bridge.client.explain(source="game/a.rpy:3")

    assert reply["script"][0]["kind"] == "If"
    assert reply["values"] == {"points": 5}
    assert not [key for key in reply if key.startswith("_")]


def test_menu_choice_conditions_report_their_values(running_bridge):
    f = "game/shop.rpy"
    running_bridge.renpy.store.gold = 12
    menu = _script_node("Menu", f, 2, items=[
        ("Pay", "gold >= 10", [_script_node("Say", f, 3, who=None, what="Paid.")]),
        ("Leave", "True", [_script_node("Say", f, 5, who=None, what="Left.")]),
    ])
    _set_script(running_bridge, {"shop": _script_node("Label", f, 1, block=[menu])})

    reply = running_bridge.client.explain(label="shop")

    assert ("choice", "Pay if gold >= 10") in [(row["kind"], row["text"]) for row in reply["statements"]]
    assert reply["values"] == {"gold": 12}


# --- names bound inside a condition ----------------------------------------


def test_names_a_comprehension_or_lambda_binds_are_not_condition_variables(running_bridge):
    names = running_bridge.globs["_renforge_condition_names"]

    assert set(names("any(q.done for q in quests)")) == {"quests"}
    assert set(names("filter(lambda x: x.ok, items)")) == {"items"}
    assert set(names("[k for k in table if k] and {v: w for v, w in pairs}")) == {"table", "pairs"}
    # Only the comprehension's own x is bound; the outer x is a store variable.
    assert set(names("x > 0 and any(x for x in xs)")) == {"x", "xs"}
    # The first iterable is read in the enclosing scope.
    assert set(names("[row for row in row]")) == {"row"}


def test_a_stale_store_name_is_not_reported_for_a_comprehension_variable(running_bridge):
    store = running_bridge.renpy.store
    store.q, store.quests = types.SimpleNamespace(done=True), []
    values = running_bridge.globs["_renforge_explain_condition_values"](
        {"_conditions": ["any(q.done for q in quests)"]})

    assert values == {"quests": []}


# --- variables is its own request -------------------------------------------


def test_variables_with_another_selector_is_refused(running_bridge):
    _set_script(running_bridge, {})

    reply = running_bridge.client.explain(label="nope", variables=["score"])

    assert reply["ok"] is False
    assert "variables" in reply["error"] and "label" in reply["error"]
    assert running_bridge.client.explain(search="x", variables=["score"])["ok"] is False


def test_more_than_a_hundred_variables_says_it_was_cut(running_bridge):
    names = ["score"] + ["missing_%03d" % number for number in range(120)]

    reply = running_bridge.client.explain(variables=names)

    assert reply["ok"] is True
    assert len(reply["variables"]) == 100 and reply["variables"]["score"] == {"value": 7}
    assert reply["truncated"] is True
    assert running_bridge.client.explain(variables=["score"])["truncated"] is False


# --- explain(variables) reads ----------------------------------------------


def test_a_class_default_of_none_is_a_value_not_a_missing_field(running_bridge):
    class Hero:
        partner = None

    running_bridge.renpy.store.hero = Hero()

    got = running_bridge.client.explain(variables=["hero.partner", "hero.rival"])["variables"]

    assert got["hero.partner"] == {"value": None}
    assert got["hero.rival"] == {"error": "no field rival"}


def test_a_long_container_is_not_copied_in_full_to_list_its_first_entries(running_bridge):
    taken = []

    class Log(list):
        def __iter__(self):
            for item in list.__iter__(self):
                taken.append(item)
                yield item

    running_bridge.renpy.store.log = Log(range(5000))

    got = running_bridge.client.explain(variables=["log"])["variables"]["log"]

    assert got["value"] == list(range(40)) and got["size"] == 5000 and got["truncated"] is True
    assert len(taken) <= 41


# --- search ----------------------------------------------------------------


def _button(line, label, children=()):
    f = "game/menu.rpy"
    return _script_node("SLDisplayable", f, line, name="textbutton", positional=["'%s'" % label],
                        keyword=[("action", "Return()")], children=list(children), location=(f, line))


def test_screen_search_lists_matches_in_document_order_and_keeps_the_first(running_bridge):
    f = "game/menu.rpy"
    _set_script(running_bridge, {})
    running_bridge.renpy.game.script.all_stmts = []
    nested = _script_node("SLIf", f, 3, entries=[("ready", _node("SLBlock", children=[_button(4, "Go on")]))])
    ast = _node("SLScreen", children=[_button(1, "Go east"), _button(2, "Go west", [nested]), _button(5, "Go home")])
    _set_screens(running_bridge, {("menu", None): _screen((f, 1), ast=ast)})

    every = running_bridge.client.explain(search="'go")
    first = running_bridge.client.explain(search="'go", limit=2)

    assert [match["line"] for match in every["matches"]] == [1, 2, 4, 5]
    assert [match["line"] for match in first["matches"]] == [1, 2] and first["truncated"] is True


def test_search_snippet_keeps_the_match_when_case_folding_changes_length(running_bridge):
    f = "game/script.rpy"
    say = _script_node("Say", f, 2, who=None, what="ß" * 80 + "TARGET")
    _set_script(running_bridge, {"start": _script_node("Label", f, 1, block=[say])})
    running_bridge.renpy.game.script.all_stmts = [say]
    _set_screens(running_bridge, {})

    [match] = running_bridge.client.explain(search="target")["matches"]

    assert "TARGET" in match["text"]


# --- progression owns its skipping -------------------------------------------


def _skip_events(running_bridge):
    return [(event["type"], event.get("reason")) for event in running_bridge.client.poll_events()["events"]
            if event["type"].startswith("skip.")]


def test_a_progression_does_not_report_its_own_skipping_as_skip_events(running_bridge, monkeypatch):
    bridge, renpy, _raw = _native_skip_setup(running_bridge, monkeypatch)
    assert renpy.config.skipping == "slow"
    time.sleep(0.1)  # the bridge's drains run with the progression's skip on

    stopped = running_bridge.client.progression("stop", "owner", reason="cancelled")
    time.sleep(0.1)

    assert stopped["progression"]["stop"] == "cancelled" and renpy.config.skipping is None
    assert _skip_events(running_bridge) == []
    # Skipping the player turns on afterwards is reported again.
    renpy.config.skipping = "fast"
    time.sleep(0.1)
    assert _skip_events(running_bridge) == [("skip.started", None)]


# --- act keeps its reply when an action steers the script ---------------------


class _JumpOut(Exception):
    pass


def _act_board(running_bridge):
    """A bar and a drag on one screen; each one's callback jumps out."""
    renpy = running_bridge.renpy

    class Adjustment:
        value, range, step, page = 2, 10, 1, 10

        def change(self, value):
            raise _JumpOut()

    class Bar:
        def __init__(self):
            self.adjustment = Adjustment()
            self.mcp_id = "volume"

    class Drag:
        drag_name, draggable, droppable = "token", True, True
        clicked, dropped = None, None
        x, y, w, h = 0, 0, 40, 40
        child = types.SimpleNamespace(filename="images/token.png")

        def dragged(self, joined, drop):
            raise _JumpOut()

    class ScreenDisplayable:
        screen_name, layer, modal, hiding = ("board", None), "screens", False, False

    token = Drag()
    screen = ScreenDisplayable()
    screen.widgets, screen.children, screen.child = {}, [token], token
    _arm_stable_interaction(running_bridge, generation=8)
    renpy.game.context = lambda: types.SimpleNamespace(interacting=True, scene_lists=types.SimpleNamespace(
        layers={"screens": [types.SimpleNamespace(displayable=screen)]}))
    renpy.game.JumpOutException = _JumpOut
    focus = _FakeFocus("", 200, 200, 80, 16, widget=Bar())
    focus.screen_name = "board"
    renpy.display.focus.focus_list[:] = [focus]
    renpy.display.behavior.run = lambda action, *args: action(*args)


@pytest.mark.parametrize("request_, expected", [
    ({"id": "board/volume", "value": 4}, {"operation": "value", "id": "board/volume", "screen": "board"}),
    ({"id": "board/token", "drop": ""}, {"operation": "drop", "id": "board/token", "screen": "board"}),
])
def test_act_value_and_drop_keep_their_reply_when_the_action_jumps(running_bridge, request_, expected):
    _act_board(running_bridge)
    with pytest.raises(_JumpOut) as raised:
        running_bridge.globs["_renforge_h_act"](dict(request_, interaction=8))

    result = getattr(raised.value, "renforge_result", None)
    assert result is not None and result["ok"] is True and result["via"] == "action"
    assert {key: result[key] for key in expected} == expected
    assert result["interaction"] == 8
