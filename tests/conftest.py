"""Shared test setup."""

from __future__ import annotations

import threading
import time
import types

import pytest
from bridge_harness import _fake_renpy, _game_store_names, _load_bridge_body

from renforge.bridge.client import BridgeClient, BridgeConfig
from renforge.state import control_dir, project_state_dir


@pytest.fixture(autouse=True)
def _isolated_renforge_state(tmp_path_factory, monkeypatch):
    # RenForge keeps per-project state outside the game; never use the real one.
    monkeypatch.setenv("RENFORGE_STATE_DIR", str(tmp_path_factory.mktemp("renforge-state")))


@pytest.fixture(autouse=True)
def _all_tools_listed(monkeypatch):
    # A developer's RENFORGE_TOOLSETS must not change what the tests see.
    monkeypatch.delenv("RENFORGE_TOOLSETS", raising=False)


@pytest.fixture(autouse=True)
def _no_ambient_isolation(monkeypatch: pytest.MonkeyPatch) -> None:
    # RENFORGE_ISOLATION changes launch defaults server-wide. A developer who
    # exports it must not change what the suite tests; tests that need it set
    # it themselves.
    monkeypatch.delenv("RENFORGE_ISOLATION", raising=False)


@pytest.fixture
def running_bridge(tmp_path, monkeypatch):
    """Run the real bridge.rpy body against a fake ``renpy`` and connect a client.

    Yields a namespace with ``client`` (a live ``BridgeClient``), ``store``,
    ``renpy`` (the fake from ``bridge_harness._fake_renpy``), ``globs`` (the
    bridge's globals), ``project_root``, ``session_id`` and ``token``. A
    thread stands in for Ren'Py's main thread and drains the request queue.
    """
    import stat

    from renforge.bridge.control import read_bridge_info, write_starting_bridge_info

    project_root = tmp_path.resolve(strict=True)
    session_id = "a" * 32
    token = "b" * 64

    write_starting_bridge_info(project_root, session_id=session_id, token=token)

    monkeypatch.setenv("RENFORGE_BRIDGE_TOKEN", token)
    monkeypatch.setenv("RENFORGE_BRIDGE_SESSION_ID", session_id)
    monkeypatch.setenv("RENFORGE_BRIDGE_PROJECT_ROOT", str(project_root))
    monkeypatch.setenv("RENFORGE_BRIDGE_PORT", "0")
    monkeypatch.setenv("RENFORGE_BRIDGE_CONTROL_DIR", str(control_dir(project_root)))

    store = types.SimpleNamespace(score=7, player_name="Rin", _hidden="x")

    class _QuickSave:
        def __call__(self):
            return ("QuickSave",)

    class _QuickLoad:
        def __call__(self, confirm=True):
            return ("QuickLoad", confirm)

    store.QuickSave = _QuickSave()
    store.QuickLoad = _QuickLoad()

    renpy = _fake_renpy(store)
    renpy.config.basedir = str(project_root)

    class _FakeEvent:
        def __init__(self, event_type, attributes=None):
            self.type = event_type
            for name, value in (attributes or {}).items():
                setattr(self, name, value)

    pygame = types.ModuleType("pygame_sdl2")
    pygame.TEXTINPUT = 1
    pygame.KEYDOWN = 2
    pygame.KEYUP = 3
    pygame.MOUSEMOTION = 6
    pygame.MOUSEBUTTONDOWN = 4
    pygame.MOUSEBUTTONUP = 5
    pygame.K_F1 = 101
    pygame.K_F2 = 102
    pygame.K_F3 = 103
    pygame.K_F4 = 104
    pygame.K_F5 = 105
    pygame.K_F6 = 106
    pygame.K_F7 = 107
    pygame.K_F8 = 108
    pygame.K_F9 = 109
    pygame.K_F10 = 110
    pygame.K_F11 = 111
    pygame.K_F12 = 112
    pygame.KMOD_NONE = 0
    pygame.event = types.SimpleNamespace(
        Event=_FakeEvent,
        post=lambda event: renpy._pygame_events.append(event),
    )
    monkeypatch.setitem(__import__("sys").modules, "pygame_sdl2", pygame)

    # Bridge keeps runtime state on a sys.modules entry so saves stay picklable.
    import sys

    sys.modules.pop("_renforge_runtime", None)

    class RevertableObject(object):
        """Ren'Py's store `object`: its changes are logged for rollback and saved."""

    globs = {"__name__": "bridge_rpy", "renpy": renpy, "object": RevertableObject, **_game_store_names()}
    exec(compile(_load_bridge_body(), "bridge.rpy", "exec"), globs)
    # Runtime state is never a revertable object, so saves never pickle it.
    assert not isinstance(sys.modules["_renforge_runtime"].bridge, RevertableObject)
    # The bridge never replaces a game's own store variables.
    assert globs["types"] == ["sword", "shield"] and globs["json"] == {"save": 1}

    runtime = sys.modules.get("_renforge_runtime")
    bridge = getattr(runtime, "bridge", None)
    assert bridge is not None, "bridge did not start"

    # Stand in for Ren'Py's main thread: keep draining the request queue.
    stop = threading.Event()

    def pump():
        while not stop.is_set():
            globs["renforge_drain_bridge"]()
            time.sleep(0.005)

    pump_thread = threading.Thread(target=pump, daemon=True)
    pump_thread.start()

    # Wait for the listener to publish ready metadata under the private control path.
    info_path = control_dir(project_root) / "bridge.json"
    ready_info = None
    for _ in range(300):
        try:
            ready_info = read_bridge_info(
                project_root,
                require_ready=True,
                expected_session_id=session_id,
            )
            break
        except Exception:
            pass
        time.sleep(0.01)
    assert ready_info is not None, "bridge did not publish valid ready metadata"
    assert ready_info.session_id == session_id
    assert ready_info.token == token
    assert ready_info.project_root == str(project_root)
    assert ready_info.host == "127.0.0.1"
    assert 1 <= ready_info.port <= 65535
    # POSIX private mode is 0600. Windows st_mode is not a real Unix mode
    # (often 0o666); ownership is enforced via the protected DACL instead.
    import os

    if os.name != "nt":
        mode = stat.S_IMODE(info_path.lstat().st_mode)
        assert mode == 0o600
    else:
        from renforge.util.files import _win_validate_protected_dacl

        _win_validate_protected_dacl(info_path)
    assert not info_path.is_symlink()
    assert not (project_state_dir(project_root) / "bridge.json").exists()

    client = BridgeClient(
        BridgeConfig(
            host=ready_info.host,
            port=ready_info.port,
            token=ready_info.token,
        )
    )
    env = types.SimpleNamespace(
        client=client,
        store=store,
        renpy=renpy,
        globs=globs,
        project_root=project_root,
        session_id=session_id,
        token=token,
    )
    yield env

    stop.set()
    bridge.stop.set()
