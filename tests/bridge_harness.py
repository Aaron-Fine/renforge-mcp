"""Fakes and helpers shared by the bridge test modules.

The Python body of ``bridge.rpy`` runs against a fake ``renpy`` module built
here; the ``running_bridge`` fixture in conftest.py starts it and connects a
real ``BridgeClient``. Test modules import these helpers from this module, never
from each other. pytest's ``pythonpath`` setting puts tests/ on ``sys.path``, so
the import works under every ``--import-mode``.
"""

from __future__ import annotations

import types
from pathlib import Path

from renforge.state import control_dir


def _load_bridge_body():
    """Return the Python body of bridge.rpy's ``init python:`` block, dedented."""
    from renforge.bridge.launcher import bridge_payload

    lines = bridge_payload().decode("utf-8").splitlines()
    assert lines[0].strip() == "init python:"
    return "\n".join(line[4:] if line.startswith("    ") else line for line in lines[1:])


class _GameCharacter:
    """A game's ``define id = Character("Id")``; calling it would say a line."""

    def __init__(self, name):
        self.name = name

    def __call__(self, what, *args, **kwargs):
        raise AssertionError("the bridge called the game's %r Character" % self.name)


def _game_store_names():
    """Store variables real games define over builtins and the bridge's imports."""
    names = {name: _GameCharacter(name) for name in ("id", "max", "min", "len", "type", "map", "next", "all")}
    names.update(types=["sword", "shield"], json={"save": 1})
    return names


class _FakeWidget:
    """A focusable widget whose text-to-speech text is *text*."""
    def __init__(self, text):
        self._text = text

    def _tts_all(self, raw=False):
        return self._text


class _FakeRect:
    """The bounding rectangle a fake surface reports."""
    def __init__(self, left, top, width, height):
        self.left = left
        self.top = top
        self.width = width
        self.height = height


class _FakeSurface:
    """A rendered surface whose opaque area is *rect*."""
    def __init__(self, rect):
        self._rect = rect

    def get_bounding_rect(self, min_alpha=1):
        return self._rect


class _FakeImageButtonWidget:
    """An image button: its label is speech text only, and it draws an opaque rectangle."""

    state_children = True

    def __init__(self, text, alpha_rect=(4, 6, 80, 14)):
        self._text = text
        self._alpha_rect = _FakeRect(*alpha_rect)
        self._child = object()
        self.style = types.SimpleNamespace(prefix="idle_")

    def _tts_all(self, raw=False):
        return self._text

    def get_child(self):
        return self._child


class _FakeFocus:
    """An entry of Ren'Py's focus list: a widget and its x, y, w, h on screen."""
    def __init__(self, text, x, y, w, h, widget=None):
        if widget is not None:
            self.widget = widget
        else:
            self.widget = _FakeWidget(text) if text is not None else None
        self.x, self.y, self.w, self.h = x, y, w, h


class _FakeInput:
    """Stands in for ``renpy.display.behavior.Input``."""


def _fake_renpy(store):
    """Build a fake ``renpy`` module over *store*: config, focus list, test mouse, image bounds and show."""
    config = types.SimpleNamespace(
        basedir="",
        label_callbacks=[],
        periodic_callbacks=[],
        all_character_callbacks=[],
        statement_callbacks=[],
        exception_handler=None,
    )
    renpy = types.ModuleType("renpy")
    renpy.store = store
    renpy.config = config
    renpy.screenshot_to_bytes = lambda size: b"\x89PNG\r\n_fake_frame_"
    renpy.get_showing_tags = lambda: ["bg", "eileen"]
    renpy._queued_events = []
    renpy._pygame_events = []
    renpy._ran_actions = []
    renpy._invoked = []
    renpy.exports = types.SimpleNamespace(
        queue_event=lambda name, **kw: renpy._queued_events.append(name)
    )
    renpy.run = lambda action, *a, **k: renpy._ran_actions.append(action) or action
    renpy.invoke_in_main_thread = lambda fn, *a, **k: renpy._invoked.append((fn, a, k)) or fn(*a, **k)
    renpy.reload_script = lambda: renpy._invoked.append(("reload_script",))
    renpy.restart_interaction = lambda: renpy._invoked.append(("restart_interaction",))
    renpy.quit = lambda: renpy._invoked.append(("quit",))

    # Minimal focus + input system mirroring Ren'Py's runtime shape.
    load_button = _FakeImageButtonWidget("Load icon")
    focus_list = [
        _FakeFocus(None, None, None, None, None),  # the "default" whole-screen focus
        _FakeFocus("Alpha choice", 10, 10, 100, 20),
        _FakeFocus("Beta choice", 10, 40, 100, 20),
        _FakeFocus("Load icon", 200, 100, 100, 30, widget=load_button),
    ]
    renpy._focused_widget = None
    renpy.display = types.SimpleNamespace(
        focus=types.SimpleNamespace(
            focus_list=focus_list,
            get_focused=lambda: renpy._focused_widget,
        ),
        behavior=types.SimpleNamespace(Input=_FakeInput),
        interface=types.SimpleNamespace(mouse_focused=False, ignore_touch=False),
    )
    renpy._clicks = []
    renpy._moves = []

    def _move_mouse(x, y):
        renpy._moves.append((x, y))
        renpy.test.testmouse.mouse_pos = (x, y)

    def _click_mouse(button, x, y):
        renpy._clicks.append((button, x, y))

    def _find_focus(pattern, raw=False):
        for focus in focus_list:
            if focus.widget is None:
                continue
            if pattern.lower() in focus.widget._tts_all(raw).lower():
                return focus
        return None

    def _find_position(focus, _position):
        return (focus.x + focus.w // 2, focus.y + focus.h // 2)

    renpy.test = types.SimpleNamespace(
        testfocus=types.SimpleNamespace(find_focus=_find_focus, find_position=_find_position),
        testmouse=types.SimpleNamespace(
            click_mouse=_click_mouse,
            move_mouse=_move_mouse,
            mouse_pos=None,
            mouse_buttons=[False, False, False],
        ),
    )

    # Displayable bounds + live repositioning. `_shown` maps a tag to its
    # rendered [x, y, w, h]; show() mutates it from the Transform's placement so
    # a reposition round-trips through get_image_bounds like the real engine.
    renpy._shown = {"eileen": [400, 300, 200, 400]}
    renpy.config.screen_width = 1920
    renpy.config.screen_height = 1080

    def _get_image_bounds(tag, layer=None):
        box = renpy._shown.get(tag)
        return tuple(box) if box else None

    renpy.get_image_bounds = _get_image_bounds

    class _FakeTransform:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    renpy.store.Transform = _FakeTransform

    def _show(name, at_list=None, layer=None, **kwargs):
        tag = str(name).split()[0]
        box = renpy._shown.setdefault(tag, [0, 0, 100, 100])
        for transform in at_list or []:
            placement = getattr(transform, "kwargs", {})
            if "xpos" in placement:
                box[0] = int(placement["xpos"])
            if "ypos" in placement:
                box[1] = int(placement["ypos"])

    renpy.show = _show

    def _render_to_surface(child, width=None, height=None, resize=True):
        for focus in focus_list:
            widget = getattr(focus, "widget", None)
            if widget is None or not callable(getattr(widget, "get_child", None)):
                continue
            if (widget.get_child() is child or widget in getattr(child, "render_of", ())) and hasattr(widget, "_alpha_rect"):
                return _FakeSurface(widget._alpha_rect)
        return _FakeSurface(_FakeRect(0, 0, 0, 0))

    renpy.render_to_surface = _render_to_surface
    return renpy


def _seed_starting_bridge(project_root: Path, *, session_id: str, token: str) -> None:
    """Write the starting bridge record the server leaves before launch."""
    from renforge.bridge.control import write_starting_bridge_info

    write_starting_bridge_info(project_root, session_id=session_id, token=token)


def _exec_bridge_with_env(tmp_path, monkeypatch, *, env: dict[str, str], seed_starting=True):
    """Start the bridge body under *env* without a client; return ``(project_root, bridge, stderr)``."""
    import io
    import sys

    project_root = tmp_path.resolve(strict=True)
    session_id = env.get("RENFORGE_BRIDGE_SESSION_ID", "a" * 32)
    token = env.get("RENFORGE_BRIDGE_TOKEN", "b" * 64)
    if seed_starting:
        _seed_starting_bridge(project_root, session_id=session_id if len(session_id) == 32 else "a" * 32, token=token if len(token) == 64 else "b" * 64)

    for key, value in env.items():
        monkeypatch.setenv(key, value)
    if "RENFORGE_BRIDGE_PROJECT_ROOT" not in env:
        monkeypatch.setenv("RENFORGE_BRIDGE_PROJECT_ROOT", str(project_root))
    if "RENFORGE_BRIDGE_PORT" not in env:
        monkeypatch.setenv("RENFORGE_BRIDGE_PORT", "0")
    if "RENFORGE_BRIDGE_CONTROL_DIR" not in env:
        monkeypatch.setenv("RENFORGE_BRIDGE_CONTROL_DIR", str(control_dir(project_root)))

    store = types.SimpleNamespace()
    renpy = _fake_renpy(store)
    renpy.config.basedir = str(project_root)

    pygame = types.ModuleType("pygame_sdl2")
    pygame.event = types.SimpleNamespace(Event=object, post=lambda *_a, **_k: None)
    monkeypatch.setitem(sys.modules, "pygame_sdl2", pygame)
    sys.modules.pop("_renforge_runtime", None)

    stderr = io.StringIO()
    monkeypatch.setattr(sys, "stderr", stderr)

    globs = {"__name__": "bridge_rpy", "renpy": renpy}
    exec(compile(_load_bridge_body(), "bridge.rpy", "exec"), globs)
    runtime = sys.modules.get("_renforge_runtime")
    bridge = getattr(runtime, "bridge", None)
    return project_root, bridge, stderr


def _arm_stable_interaction(running_bridge, generation=1):
    """Make the fake engine report an ongoing, settled interaction with an empty focus list; return the bridge."""
    import sys

    renpy = running_bridge.renpy
    renpy.game = types.SimpleNamespace(
        context=lambda: types.SimpleNamespace(
            interacting=True,
            scene_lists=types.SimpleNamespace(layers={}),
        )
    )
    renpy.display.interface.ongoing_transition = {}
    renpy.display.focus.focus_list[:] = []
    bridge = sys.modules["_renforge_runtime"].bridge
    bridge.observe_generation = generation
    return bridge


def _native_skip_setup(running_bridge, monkeypatch):
    """Start a native-skip progression over a fixed observe reply; return ``(bridge, renpy, raw)``."""
    import sys
    bridge = sys.modules["_renforge_runtime"].bridge
    renpy = running_bridge.renpy
    renpy.config.skipping = None
    renpy.config.allow_skipping = True
    renpy.store._skipping = True
    renpy.store._preferences = types.SimpleNamespace(skip_unseen=False, skip_after_choices=True)
    raw = {"ok": True, "interaction": bridge.observe_generation, "stable": True,
           "session_id": bridge.session_id, "observation_id": "initial", "screens": [],
           "elements": [], "say_dismiss": "dismiss", "dialogue": None}
    monkeypatch.setitem(running_bridge.globs, "_renforge_observe_raw", lambda *a, **k: dict(raw))
    reply = running_bridge.client.progression("start", "owner", interaction=raw["interaction"],
        session_id=bridge.session_id, observation_id="initial", max_steps=2, timeout=10)
    assert reply["ok"], reply
    return bridge, renpy, raw


def _completed_render(width, height, widgets=(), children=(), reverse=None, **extra):
    """A finished render: its size, the widgets it renders and its placed ``children``."""
    # A leaf stands for an image or solid: it draws through its mesh.
    extra.setdefault("mesh", not children)
    return types.SimpleNamespace(width=width, height=height, render_of=list(widgets),
        children=list(children), reverse=reverse, **extra)


def _node(kind: str, **attrs):
    """A syntax-tree node whose class is named *kind* (the bridge dispatches on class names)."""
    node = type(kind, (), {})()
    for key, value in attrs.items():
        setattr(node, key, value)
    return node


def _script_node(kind: str, filename: str, line: int, **attrs):
    """A script statement node at *filename*:*line*."""
    return _node(kind, filename=filename, linenumber=line, **attrs)


def _set_script(bridge, labels: dict) -> None:
    """Install *labels* (name to node) as the loaded script's namemap."""
    bridge.renpy.game = types.SimpleNamespace(script=types.SimpleNamespace(namemap=labels))


def _set_screens(bridge, screens: dict) -> None:
    """Install *screens* as the engine's registered screens."""
    bridge.renpy.display.screen = types.SimpleNamespace(screens=screens)


def _screen(location, ast=None, parameters=None):
    """A registered screen for explain: where it is defined, its tree and its parameters."""
    return types.SimpleNamespace(location=location, ast=ast, parameters=parameters)
