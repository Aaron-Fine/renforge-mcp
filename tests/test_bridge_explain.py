"""Exercise the bridge's ``explain`` handler against fake syntax trees.

Nodes are built from ``type(<Ren'Py class name>, (), {})`` so the bridge's
class-name dispatch sees the same names the engine produces.
"""

from __future__ import annotations

import types

import pytest

from test_bridge_runtime import running_bridge  # noqa: F401 - shared bridge fixture


def _node(kind: str, **attrs):
    node = type(kind, (), {})()
    for key, value in attrs.items():
        setattr(node, key, value)
    return node


def _script_node(kind: str, filename: str, line: int, **attrs):
    return _node(kind, filename=filename, linenumber=line, **attrs)


def _set_script(bridge, labels: dict) -> None:
    bridge.renpy.game = types.SimpleNamespace(script=types.SimpleNamespace(namemap=labels))


def _set_screens(bridge, screens: dict) -> None:
    bridge.renpy.display.screen = types.SimpleNamespace(screens=screens)


def _screen(location, ast=None, parameters=None):
    return types.SimpleNamespace(location=location, ast=ast, parameters=parameters)


def _rows(reply):
    return [(row["kind"], row["depth"], row["text"]) for row in reply["statements"]]


# --- selector validation -------------------------------------------------


def test_explain_rejects_more_than_one_selector(running_bridge):
    reply = running_bridge.client.explain(label="start", screen="rf_hub")

    assert reply == {"ok": False, "error": "pass at most one of label, screen, id, source"}


def test_explain_rejects_non_string_selector(running_bridge):
    reply = running_bridge.client.explain(label=5)

    assert reply["ok"] is False
    assert "label must be a string" in reply["error"]


def test_explain_unknown_label_and_screen_are_errors(running_bridge):
    _set_script(running_bridge, {})
    _set_screens(running_bridge, {})

    assert running_bridge.client.explain(label="nope") == {"ok": False, "error": "unknown label"}
    assert running_bridge.client.explain(screen="nope") == {"ok": False, "error": "unknown screen"}


# --- index ---------------------------------------------------------------


def test_index_sorts_labels_filters_private_names_and_reports_files(running_bridge):
    game_dir = running_bridge.project_root / "game"
    game_dir.mkdir()
    (game_dir / "script.rpy").write_text("label start:\n", encoding="utf-8")
    _set_script(running_bridge, {
        "start": _script_node("Label", "game/script.rpy", 10),
        "zeta": _script_node("Label", "game/a.rpy", 3),
        "beta": _script_node("Label", "game/script.rpy", 2),
        "_hidden": _script_node("Label", "game/script.rpy", 1),
        "gone": _node("Image"),
    })
    _set_screens(running_bridge, {
        ("rf_hub", None): _screen(("game/screens.rpy", 4)),
        ("choice", "d"): _screen(("game/screens.rpy", 1), parameters=None),
        ("_internal", None): _screen(("game/screens.rpy", 2)),
    })
    running_bridge.renpy.config.layers = ["master"]
    running_bridge.renpy.config.top_layers = ["overlay"]
    running_bridge.renpy.get_screen = lambda name, layer=None: (
        object() if name == "rf_hub" and layer == "overlay" else None
    )

    reply = running_bridge.client.explain()

    assert reply["ok"] is True
    assert reply["truncated"] is False
    assert (reply["label_total"], reply["screen_total"]) == (3, 2)
    assert [(item["name"], item["file"], item["line"]) for item in reply["labels"]] == [
        ("zeta", "game/a.rpy", 3),
        ("beta", "game/script.rpy", 2),
        ("start", "game/script.rpy", 10),
    ]
    assert [(item["name"], item["variant"], item["file"], item["shown_on"]) for item in reply["screens"]] == [
        ("choice", "d", "game/screens.rpy", []),
        ("rf_hub", None, "game/screens.rpy", ["overlay"]),
    ]
    assert reply["screens"][0]["parameters"] is None
    assert reply["files"] == {"total": 3, "on_disk": 1}

    filtered = running_bridge.client.explain(query="HUB")
    assert [item["name"] for item in filtered["screens"]] == ["rf_hub"]
    assert filtered["labels"] == [] and filtered["query"] == "HUB"


def test_index_is_bounded_by_limit_and_flags_truncation(running_bridge):
    labels = {
        "label_%04d" % number: _script_node("Label", "game/big.rpy", number + 1)
        for number in range(250)
    }
    _set_script(running_bridge, labels)
    _set_screens(running_bridge, {})

    reply = running_bridge.client.explain()

    assert reply["truncated"] is True
    assert reply["label_total"] == 250
    assert len(reply["labels"]) == 200
    assert reply["labels"][0]["line"] == 1
    assert len(running_bridge.client.explain(limit=5)["labels"]) == 5


# --- label walk ----------------------------------------------------------


def _label_fixture():
    f = "game/script.rpy"
    block = [
        _script_node("Say", f, 2, who="e", what="Hello."),
        _script_node("Menu", f, 3, items=[
            ("Open", "rf_gate", [_script_node("Jump", f, 5, target="rf_room_expr", expression=True)]),
            ("Wait", "True", [_script_node("Say", f, 7, who=None, what="Waiting.")]),
            ("Caption text", None, None),
        ]),
        _script_node("If", f, 8, entries=[
            ("x > 1", [_script_node("Say", f, 9, who=None, what="big")]),
            ("x > 0", [_script_node("Say", f, 10, who=None, what="small")]),
            (None, [_script_node("Say", f, 11, who=None, what="none")]),
        ]),
        _script_node("Translate", f, 12, block=[
            _script_node("Say", f, 13, who="e", what="Translated."),
            _script_node("EndTranslate", f, 14),
        ]),
        _script_node("Python", f, 15, code=types.SimpleNamespace(source="\nx = 1\ny = 2")),
        _script_node("Show", f, 16, imspec=(("eileen", "happy"), None, None, None, None, None)),
        _script_node("Call", f, 17, label="sub", expression=True),
        _script_node("Label", f, 18, name="inner", block=[]),
        _script_node("Pass", f, 19),
        _script_node("Return", f, 20, expression=None),
    ]
    return _script_node("Label", f, 1, block=block)


def test_label_walk_flattens_translate_and_keeps_menu_and_if_structure(running_bridge):
    _set_script(running_bridge, {"start": _label_fixture()})

    reply = running_bridge.client.explain(label="start")

    assert reply["ok"] is True
    assert reply["label"] == "start"
    assert reply["file"] == "game/script.rpy"
    assert reply["line"] == 1
    assert reply["truncated"] is False
    assert _rows(reply) == [
        ("say", 0, "e: Hello."),
        ("menu", 0, ""),
        ("choice", 0, "Open if rf_gate"),
        ("jump", 1, "expression rf_room_expr"),
        ("choice", 0, "Wait"),
        ("say", 1, "Waiting."),
        ("caption", 0, "Caption text"),
        ("if", 0, "x > 1"),
        ("say", 1, "big"),
        ("elif", 0, "x > 0"),
        ("say", 1, "small"),
        ("else", 0, ""),
        ("say", 1, "none"),
        ("say", 0, "e: Translated."),
        ("python", 0, "x = 1"),
        ("show", 0, "eileen happy"),
        ("call", 0, "expression sub"),
        ("label", 0, "inner"),
        ("return", 0, ""),
    ]
    choice = reply["statements"][2]
    assert (choice["file"], choice["line"]) == ("game/script.rpy", 3)


def test_label_walk_respects_limit(running_bridge):
    _set_script(running_bridge, {"start": _label_fixture()})

    reply = running_bridge.client.explain(label="start", limit=3)

    assert len(reply["statements"]) == 3
    assert reply["truncated"] is True


def test_label_limit_is_clamped_to_at_least_one(running_bridge):
    _set_script(running_bridge, {"start": _label_fixture()})

    reply = running_bridge.client.explain(label="start", limit=0)

    assert len(reply["statements"]) == 1
    assert reply["truncated"] is True


def test_label_walk_shortens_long_text(running_bridge):
    long_line = "x" * 400
    block = [_script_node("Say", "game/script.rpy", 2, who=None, what=long_line)]
    _set_script(running_bridge, {"start": _script_node("Label", "game/script.rpy", 1, block=block)})

    reply = running_bridge.client.explain(label="start")

    text = reply["statements"][0]["text"]
    assert len(text) == 160
    assert text.endswith("...")


def test_label_walk_skips_a_broken_node_and_keeps_going(running_bridge):
    broken = _script_node("Menu", "game/script.rpy", 2, items=[("bad", None)])
    block = [broken, _script_node("Say", "game/script.rpy", 3, who=None, what="after")]
    _set_script(running_bridge, {"start": _script_node("Label", "game/script.rpy", 1, block=block)})

    reply = running_bridge.client.explain(label="start")

    assert ("say", 0, "after") in _rows(reply)


# --- screen walk ---------------------------------------------------------


def _screen_fixture():
    f = "game/screens.rpy"
    door = _script_node("SLDisplayable", f, 6, name="textbutton", positional=["'Door'"],
                        keyword=[("action", 'Jump("rf_room")'), ("style", "x")], children=[], location=(f, 6))
    look = _script_node("SLDisplayable", f, 8, name="textbutton", positional=["'Look'"],
                        keyword=[("action", "NullAction()")], children=[], location=(f, 8))
    inner = _script_node("SLDisplayable", f, 10, name="text", positional=["str(i)"], keyword=[],
                         children=[], location=(f, 10))
    def frame_fn():
        return None

    frame = _script_node("SLDisplayable", f, 11, name=frame_fn, positional=[], keyword=[],
                         children=[])
    tail = _script_node("SLDisplayable", f, 12, name="text", positional=["'Inside'"], keyword=[],
                        children=[])
    return _node("SLScreen", children=[
        _script_node("SLIf", f, 5, entries=[
            ("rf_gate", _node("SLBlock", children=[door])),
            (None, _node("SLBlock", children=[look])),
        ]),
        _script_node("SLFor", f, 9, variable="i", expression="range(2)", children=[inner]),
        _script_node("SLUse", f, 13, target="rf_common", block=_node("SLBlock", children=[
            _script_node("SLDefault", f, 14, variable="z", expression="1"),
        ])),
        _script_node("SLPython", f, 15, code=types.SimpleNamespace(source="v = 1")),
        frame,
        _node("SLBlock", children=[tail]),
    ])


def test_screen_walk_covers_if_for_use_and_python(running_bridge):
    _set_screens(running_bridge, {
        ("rf_hub", None): _screen(("game/screens.rpy", 4), ast=_screen_fixture()),
        ("py_screen", None): _screen(("game/screens.rpy", 30), ast=None),
    })

    reply = running_bridge.client.explain(screen="rf_hub")

    assert reply["ok"] is True
    assert reply["screen"] == "rf_hub"
    assert reply["variant"] is None
    assert reply["file"] == "game/screens.rpy"
    assert reply["line"] == 4
    assert reply["truncated"] is False
    assert [(node["kind"], node["depth"], node["text"]) for node in reply["nodes"]] == [
        ("if", 0, "rf_gate"),
        ("textbutton", 1, "'Door' action Jump(\"rf_room\")"),
        ("else", 0, ""),
        ("textbutton", 1, "'Look' action NullAction()"),
        ("for", 0, "i in range(2)"),
        ("text", 1, "str(i)"),
        ("use", 0, "rf_common"),
        ("default", 1, "z = 1"),
        ("python", 0, "v = 1"),
        ("frame_fn", 0, ""),
        ("text", 0, "'Inside'"),
    ]


def test_python_defined_screen_has_no_nodes(running_bridge):
    _set_screens(running_bridge, {("py_screen", None): _screen(("game/screens.rpy", 30), ast=None)})

    reply = running_bridge.client.explain(screen="py_screen")

    assert reply["ok"] is True
    assert reply["nodes"] == []
    assert reply["reason"] == "screen defined in Python"


def test_screen_walk_respects_limit(running_bridge):
    _set_screens(running_bridge, {("rf_hub", None): _screen(("game/screens.rpy", 4), ast=_screen_fixture())})

    reply = running_bridge.client.explain(screen="rf_hub", limit=2)

    assert len(reply["nodes"]) == 2
    assert reply["truncated"] is True


# --- control resolution --------------------------------------------------


def _control_fixture(running_bridge, *, widget, screens=None):
    element = {"id": "door", "text": "Door", "screen": "rf_hub", "source": "game/screens.rpy:9"}
    focus = types.SimpleNamespace(widget=widget)
    running_bridge.globs["_renforge_resolve_ui_element"] = (
        lambda payload, action: (focus, element, None)
    )
    _set_screens(running_bridge, screens or {})


def _rf_hub_ast():
    f = "game/screens.rpy"
    door = _script_node("SLDisplayable", f, 9, name="textbutton", positional=["'Door'"],
                        keyword=[("action", 'Jump("rf_room")')], children=[], location=(f, 9))
    loop = _script_node("SLFor", f, 8, variable="i", expression="range(10)", children=[door])
    return _node("SLScreen", children=[
        _script_node("SLIf", f, 6, entries=[
            ("quest_stage >= 3", _node("SLBlock", children=[loop])),
            (None, _node("SLBlock", children=[])),
        ]),
    ])


def test_explain_control_traces_the_statement_and_its_conditions(running_bridge):
    widget = types.SimpleNamespace(_main=types.SimpleNamespace(_location=("game/screens.rpy", 9)))
    _control_fixture(running_bridge, widget=widget,
                     screens={("rf_hub", None): _screen(("game/screens.rpy", 4), ast=_rf_hub_ast())})

    reply = running_bridge.client.explain(id="door")

    assert reply == {
        "ok": True,
        "control": {"id": "door", "text": "Door", "screen": "rf_hub", "source": "game/screens.rpy:9"},
        "defined_in_screen": "rf_hub",
        "statement": {
            "kind": "textbutton",
            "file": "game/screens.rpy",
            "line": 9,
            "positional": ["'Door'"],
            "keywords": {"action": 'Jump("rf_room")'},
        },
        "conditions": ["if quest_stage >= 3", "for i in range(10)"],
    }


def test_explain_control_follows_use_into_the_used_screen(running_bridge):
    f = "game/common.rpy"
    shared = _script_node("SLDisplayable", f, 2, name="textbutton", positional=["'Shared'"],
                          keyword=[], children=[], location=(f, 2))
    ast = _node("SLScreen", children=[
        _script_node("SLUse", "game/screens.rpy", 3, target="rf_common",
                     block=_node("SLBlock", children=[shared])),
    ])
    widget = types.SimpleNamespace(_main=types.SimpleNamespace(_location=(f, 2)))
    _control_fixture(running_bridge, widget=widget,
                     screens={("rf_hub", None): _screen(("game/screens.rpy", 1), ast=ast)})

    reply = running_bridge.client.explain(id="door")

    assert reply["statement"]["kind"] == "textbutton"
    assert reply["conditions"] == ["use rf_common"]


def test_explain_control_built_in_python_has_no_statement(running_bridge):
    _control_fixture(running_bridge, widget=types.SimpleNamespace(), screens={})

    reply = running_bridge.client.explain(id="door")

    assert reply["ok"] is True
    assert reply["statement"] is None
    assert reply["reason"] == "built in Python; no screen-language location"
    assert reply["control"]["id"] == "door"


def test_explain_control_location_not_in_any_screen(running_bridge):
    widget = types.SimpleNamespace(_location=("game/elsewhere.rpy", 1))
    _control_fixture(running_bridge, widget=widget,
                     screens={("rf_hub", None): _screen(("game/screens.rpy", 4), ast=_rf_hub_ast())})

    reply = running_bridge.client.explain(id="door")

    assert reply["ok"] is True
    assert reply["statement"] is None
    assert reply["reason"] == "location not found in screen definitions"


def test_explain_control_surfaces_resolver_errors(running_bridge):
    running_bridge.globs["_renforge_resolve_ui_element"] = (
        lambda payload, action: (None, None, {"ok": False, "error": "no UI element matching None/'door'"})
    )

    reply = running_bridge.client.explain(id="door")

    assert reply == {"ok": False, "error": "no UI element matching None/'door'"}


def test_compiled_script_names_are_reported_as_source_names(running_bridge):
    block = [_script_node("Say", "game/script.rpyc", 2, who=None, what="hi")]
    label = _script_node("Label", "game/script.rpyc", 1, block=block)
    _set_script(running_bridge, {"start": label})

    reply = running_bridge.client.explain(label="start")

    assert reply["file"] == "game/script.rpy"
    assert reply["statements"][0]["file"] == "game/script.rpy"


def test_explain_source_traces_a_file_line_from_observe(running_bridge):
    _set_screens(running_bridge, {("rf_hub", None): _screen(("game/screens.rpy", 4), ast=_rf_hub_ast())})

    reply = running_bridge.client.explain(source="game/screens.rpy:9")

    assert reply["ok"] is True
    assert reply["source"] == "game/screens.rpy:9"
    assert reply["statement"]["keywords"] == {"action": 'Jump("rf_room")'}
    assert reply["conditions"] == ["if quest_stage >= 3", "for i in range(10)"]
    assert running_bridge.client.explain(source="nowhere")["ok"] is False
    assert running_bridge.client.explain(source="game/screens.rpy:99")["statement"] is None


def test_explain_text_collapses_whitespace(running_bridge):
    assert running_bridge.globs["_renforge_explain_text"]("A(\n    key='x',\n)") == "A( key='x', )"
