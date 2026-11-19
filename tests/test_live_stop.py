"""Tests for stopping a game through the published bridge (cross-process)."""

from __future__ import annotations

import json
import tempfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from renforge.bridge.client import BridgeError
from renforge.save_isolation import LaunchIsolation, host_renpy_root
from renforge.tools import live
from renforge.state import control_dir, project_state_dir


def _session_id(label: str = "session") -> str:
    import hashlib
    return hashlib.sha256(label.encode("utf-8")).hexdigest()[:32]


def _token(label: str = "token") -> str:
    import hashlib
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


def _make_project(tmp_path: Path, *, with_ready_bridge: bool = False, with_legacy_bridge: bool = False) -> Path:
    project = tmp_path / "project"
    (project / "game").mkdir(parents=True)
    (project / "game" / "script.rpy").write_text("label start:\n    return\n", encoding="utf-8")
    if with_legacy_bridge:
        renforge = project_state_dir(project)
        renforge.mkdir(parents=True)
        (renforge / "bridge.json").write_text(
            json.dumps({"host": "127.0.0.1", "port": 65123, "token": "t", "pid": 4242}),
            encoding="utf-8",
        )
        (project / "game" / "renforge_bridge.rpy").write_text("# injected\n", encoding="utf-8")
        (project / "game" / "renforge_bridge.rpyc").write_bytes(b"\x00")
    if with_ready_bridge:
        from renforge.bridge.control import ensure_control_dir
        from renforge.util.files import atomic_write_private_json

        ensure_control_dir(project)
        session_id = _session_id(str(project))
        token = _token(str(project))
        atomic_write_private_json(
            control_dir(project) / "bridge.json",
            {
                "schema_version": 1,
                "protocol_version": 1,
                "state": "ready",
                "session_id": session_id,
                "project_root": str(project.resolve()),
                "host": "127.0.0.1",
                "port": 65123,
                "token": token,
            },
            max_bytes=16 * 1024,
        )
        (project / "game" / f"zzrenforge_bridge_{session_id}.rpy").write_text("# owned\n", encoding="utf-8")
    return project


def _patch_bridge_client(monkeypatch, client) -> None:
    """stop_external constructs BridgeClient(config); patch the constructor."""

    def _factory(config):
        return client

    monkeypatch.setattr(live, "BridgeClient", _factory)
    monkeypatch.setattr("renforge.tools.live.BridgeClient", _factory)


def test_registry_key_applies_normcase(tmp_path: Path, monkeypatch) -> None:
    project = _make_project(tmp_path)
    monkeypatch.setattr(
        live.os.path,
        "normcase",
        lambda value: f"normalized:{value}",
    )

    assert live._key(project) == f"normalized:{project.resolve()}"


class _QuitClient:
    def __init__(self) -> None:
        self.control_calls: list[str] = []

    def control(self, action: str, **_kwargs) -> dict:
        self.control_calls.append(action)
        return {"ok": True, "action": action}

    def ping(self) -> dict:
        raise ConnectionRefusedError("bridge stopped")


class _AliveClient(_QuitClient):
    def ping(self) -> dict:
        return {"ok": True, "pong": True}


class _DeadClient:
    def control(self, action: str, **_kwargs) -> dict:
        raise ConnectionRefusedError("no bridge")

    def ping(self) -> dict:
        raise ConnectionRefusedError("no bridge")


class _StateClient(_AliveClient):
    def get_state(self) -> dict:
        return {"current_label": "dashboard_scene"}


class _LaunchedSession:
    display_mode = "native"
    startup_ms = 10
    phases: list[dict] = []
    environment: dict = {}
    temporary_savedir = None
    headless = False

    def __init__(self, *, editor: bool = False) -> None:
        self.client = _StateClient()
        self.editor = editor


class _RunningProcess:
    def poll(self) -> None:
        return None


# A session root as RenForge makes one; it need not exist to be classified.
_SESSION_ROOT = Path(tempfile.gettempdir()) / "renforge-session-test"


def _isolation(
    savedir_mode: str = "temporary",
    *,
    home_mode: str | None = None,
    savedir: Path | None = None,
    home: Path | None = None,
) -> LaunchIsolation:
    """An owned session's isolation record; isolated components live in a session root."""
    if home_mode is None:
        home_mode = "existing" if savedir_mode == "existing" else ("path" if home is not None else "temporary")
    if savedir is None and savedir_mode in ("temporary", "path"):
        savedir = _SESSION_ROOT / "saves"
    if home is None and home_mode in ("temporary", "path"):
        home = _SESSION_ROOT / "home"
    return LaunchIsolation(
        savedir_mode=savedir_mode,
        home_mode=home_mode,
        persistent_mode="empty" if savedir_mode == "temporary" else "existing",
        preferences_mode="empty" if savedir_mode == "temporary" else "existing",
        savedir=savedir,
        home=home,
        session_root=_SESSION_ROOT if savedir_mode == "temporary" or home_mode == "temporary" else None,
        cleanup=False,
    )


@pytest.fixture(autouse=True)
def _never_launch_a_real_game(monkeypatch):
    """A reuse that falls through would start the real SDK; tests that launch patch this themselves."""
    monkeypatch.setattr(live, "get_or_install_sdk",
                        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("must not launch a game")))


class _OwnedRunningSession:
    def __init__(self, *, editor: bool = False, isolation: LaunchIsolation | None = None,
                 bridge_eval: bool = True) -> None:
        self.editor = editor
        self.isolation = isolation or _isolation()
        self.client = _StateClient()
        self.process = _RunningProcess()
        self.bridge_eval = bridge_eval
        self.close_calls = 0
        self.closed = False

    def close(self) -> dict:
        self.close_calls += 1
        self.closed = True
        return {"cleaned": {"renpy_process": True}, "failed": []}


class _RetryingSession:
    def __init__(self, *, process_exited: bool = False, editor: bool = True) -> None:
        self.process_exited = process_exited
        self.editor = editor
        self.closed = False
        self.close_calls = 0

    def close(self) -> dict:
        self.close_calls += 1
        if not self.process_exited:
            return {"cleaned": {}, "failed": ["process_alive"]}
        self.closed = True
        return {"cleaned": {"renpy_process": True}, "failed": []}


class _RaisingSession:
    closed = False
    editor = True

    def close(self) -> dict:
        raise RuntimeError("teardown failed")


def _wait_for_launch_status(
    project: Path,
    expected: str,
    *,
    timeout: float = 1.0,
) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = live.launch_status(str(project))
        if result["status"] == expected:
            return result
        time.sleep(0.01)
    raise AssertionError(f"launch status did not become {expected!r}")


def test_stop_game_without_bridge_is_noop(tmp_path: Path) -> None:
    project = _make_project(tmp_path)
    assert live.stop_game(str(project)) == {
        "ok": True,
        "was_running": False,
        "files_deleted": [],
    }


def test_stop_game_issues_authenticated_quit_when_bridge_alive(
    tmp_path: Path,
    monkeypatch,
) -> None:
    project = _make_project(tmp_path, with_ready_bridge=True)
    client = _QuitClient()
    _patch_bridge_client(monkeypatch, client)

    result = live.stop_game(str(project))

    assert result == {"ok": True, "was_running": True, "files_deleted": []}
    assert client.control_calls == ["quit"]


def test_stop_game_ignores_legacy_bridge_metadata_without_live_client(
    tmp_path: Path,
    monkeypatch,
) -> None:
    project = _make_project(tmp_path, with_legacy_bridge=True)

    def missing_client(cls, root, *, timeout=5.0):
        raise live.BridgeError("bridge metadata failed validation")

    monkeypatch.setattr(
        live.BridgeClient,
        "from_project",
        classmethod(missing_client),
    )

    result = live.stop_game(str(project))

    assert result == {"ok": True, "was_running": False, "files_deleted": []}
    assert (project_state_dir(project) / "bridge.json").exists()
    assert (project / "game" / "renforge_bridge.rpy").exists()
    assert (project / "game" / "renforge_bridge.rpyc").exists()


def test_external_stop_does_not_preacquire_owner_held_lock(
    tmp_path: Path,
    monkeypatch,
) -> None:
    project = _make_project(tmp_path, with_ready_bridge=True)
    client = _QuitClient()
    _patch_bridge_client(monkeypatch, client)

    # Owner-held lock must not block authenticated quit.
    from renforge.bridge.launcher import ProjectBridgeLock

    project_lock = ProjectBridgeLock(project)
    project_lock.acquire()
    try:
        result = live.stop_external_game(str(project))
    finally:
        project_lock.release()

    # Quit must still be issued without waiting for the owner lock first.
    assert client.control_calls == ["quit"]
    assert result["was_running"] is True
    # Owner still holds the lock: cleanup is deferred / reported locked, never forced.
    assert result.get("code") == "BRIDGE_PROJECT_LOCKED" or result.get("replacement_running") is True


def test_external_stop_reports_not_running_when_quit_cannot_authenticate(
    tmp_path: Path,
    monkeypatch,
) -> None:
    project = _make_project(tmp_path, with_ready_bridge=True)
    client = _DeadClient()
    _patch_bridge_client(monkeypatch, client)

    result = live.stop_external_game(str(project))

    assert result["ok"] is False
    assert result["was_running"] is False
    assert "ConnectionRefusedError" in result["error"]


def test_external_stop_requires_explicit_authenticated_quit_ack(
    tmp_path: Path,
    monkeypatch,
) -> None:
    project = _make_project(tmp_path, with_ready_bridge=True)

    class CustomReplyClient:
        def __init__(self, reply):
            self.reply = reply

        def control(self, action: str, **_kwargs):
            return self.reply

    # Test empty dict reply
    _patch_bridge_client(monkeypatch, CustomReplyClient({}))
    result = live.stop_external_game(str(project))
    assert result == {"ok": False, "error": "bridge quit failed", "was_running": False}

    # Test missing or mismatched action reply
    _patch_bridge_client(monkeypatch, CustomReplyClient({"ok": True, "action": "wrong"}))
    result = live.stop_external_game(str(project))
    assert result == {"ok": False, "error": "bridge quit failed", "was_running": False}

    # Test explicit error reply
    _patch_bridge_client(monkeypatch, CustomReplyClient({"ok": False, "error": "custom failure"}))
    result = live.stop_external_game(str(project))
    assert result == {"ok": False, "error": "custom failure", "was_running": False}

    # Test valid quit ack
    _patch_bridge_client(monkeypatch, CustomReplyClient({"ok": True, "action": "quit"}))
    result = live.stop_external_game(str(project))
    assert result["ok"] is True
    assert result["was_running"] is True



def test_launch_reuses_a_game_started_by_the_dashboard(tmp_path: Path, monkeypatch) -> None:
    project = _make_project(tmp_path)
    monkeypatch.setattr(
        live.BridgeClient,
        "from_project",
        classmethod(lambda cls, root, **_kwargs: _StateClient()),
    )
    monkeypatch.setattr(
        live,
        "get_or_install_sdk",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("must not relaunch")),
    )

    result = live.launch_game(str(project))

    assert result["ok"] is False
    assert result["ready"] is False
    assert result["code"] == "SESSION_MODE_MISMATCH"
    assert result["requested_editor"] is True
    assert result["existing_editor"] is False
    assert "cannot be proven for an external session" in result["message"]


class _InfoClient(_StateClient):
    def __init__(self, info: dict | None) -> None:
        self._info = info

    def session_info(self) -> dict:
        if self._info is None:
            raise BridgeError("bridge error on 'session_info': unknown command")
        return dict(self._info)


def _host_home(tmp_path: Path, monkeypatch) -> Path:
    # tmp_path stands in for the temp dir, so tmp_path/renforge-session-* is a
    # session root and anything else under it is not.
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    host_home = tmp_path / "host-home"
    host_home.mkdir()
    monkeypatch.setenv("HOME", str(host_home))
    monkeypatch.setenv("USERPROFILE", str(host_home))
    monkeypatch.setenv("APPDATA", str(host_home / "AppData" / "Roaming"))
    return host_home


def _external_reuse(tmp_path: Path, monkeypatch, client, **launch_kwargs) -> dict:
    project = _make_project(tmp_path)
    monkeypatch.delenv("RENFORGE_ISOLATION", raising=False)
    monkeypatch.setattr(
        live.BridgeClient,
        "from_project",
        classmethod(lambda cls, root, **_kwargs: client),
    )
    monkeypatch.setattr(
        live,
        "get_or_install_sdk",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("must not relaunch")),
    )
    return live.launch_game(str(project), editor=False, **launch_kwargs)


@pytest.mark.parametrize(
    "info",
    [
        None,  # an older bridge that cannot say where it saves
        {"savedir": None, "home": None},
        {"savedir": "SESSION_SAVES", "home": "HOST_HOME", "save_locations": ["SESSION_SAVES"]},
        {"savedir": "HOST_SAVES", "home": "SESSION_HOME", "save_locations": ["HOST_SAVES"]},
        # config.extra_savedirs pointing anywhere outside the session root may
        # hold the player's saves, even somewhere RenForge does not recognize.
        {"savedir": "SESSION_SAVES", "home": "SESSION_HOME", "save_locations": ["SESSION_SAVES", "ELSEWHERE"]},
        # The shared temp dir itself is not a session root.
        {"savedir": "SESSION_SAVES", "home": "SESSION_HOME", "save_locations": ["TEMP_ROOT"]},
        # A path that cannot be resolved is unknown, so it is not safe.
        {"savedir": "SESSION_SAVES", "home": "SESSION_HOME", "save_locations": ["SESSION_SAVES", "bad\0path"]},
        {"savedir": "SESSION_SAVES", "home": "bad\0path", "save_locations": ["SESSION_SAVES"]},
    ],
)
def test_an_isolated_launch_is_not_handed_an_external_game_it_cannot_vouch_for(
    tmp_path: Path, monkeypatch, info
) -> None:
    host_home = _host_home(tmp_path, monkeypatch)
    host_saves = host_renpy_root() / "demo"
    session = tmp_path / "renforge-session-x"
    for path in (host_saves, session / "saves", session / "home"):
        path.mkdir(parents=True)
    if info is not None:
        mapping = {
            "SESSION_SAVES": str(session / "saves"),
            "SESSION_HOME": str(session / "home"),
            "HOST_HOME": str(host_home),
            "HOST_SAVES": str(host_saves),
            "ELSEWHERE": str(tmp_path / "Documents" / "OtherSaves"),
            "TEMP_ROOT": str(tmp_path),
        }

        def resolve(value):
            if isinstance(value, list):
                return [mapping.get(item, item) for item in value]
            return mapping.get(value, value)

        info = {key: resolve(value) for key, value in info.items()}

    result = _external_reuse(tmp_path, monkeypatch, _InfoClient(info), savedir="temporary")

    assert result["ok"] is False
    assert result["code"] == "SESSION_ISOLATION_MISMATCH"
    assert result["existing_exposes_user_saves"] is True


def test_an_isolated_launch_reuses_an_isolated_external_game(tmp_path: Path, monkeypatch) -> None:
    _host_home(tmp_path, monkeypatch)
    session = tmp_path / "renforge-session-x"
    for path in (session / "saves", session / "home"):
        path.mkdir(parents=True)
    client = _InfoClient({
        "savedir": str(session / "saves"),
        "home": str(session / "home"),
        "save_locations": [str(session / "saves")],
    })

    result = _external_reuse(tmp_path, monkeypatch, client, savedir="temporary")

    assert result["ok"] is True
    assert result["external"] is True
    assert result["current_label"] == "dashboard_scene"
    assert result["isolation"] == {
        "savedir": str(session / "saves"),
        "home": str(session / "home"),
        "save_locations": [str(session / "saves")],
        "exposes_user_saves": False,
    }


def test_an_isolated_launch_refuses_an_external_game_that_also_uses_game_saves(
    tmp_path: Path, monkeypatch
) -> None:
    # The reported savedir is private, but Ren'Py still writes to every
    # registered location, and this game kept the project's game/saves.
    _host_home(tmp_path, monkeypatch)
    session = tmp_path / "renforge-session-x"
    for path in (session / "saves", session / "home"):
        path.mkdir(parents=True)
    project_saves = tmp_path / "project" / "game" / "saves"  # _make_project's layout
    client = _InfoClient({
        "savedir": str(session / "saves"),
        "home": str(session / "home"),
        "save_locations": [str(session / "saves"), str(project_saves)],
    })

    result = _external_reuse(
        tmp_path, monkeypatch, client, savedir="temporary", language="japanese"
    )

    assert result["ok"] is False
    assert result["code"] == "SESSION_ISOLATION_MISMATCH"
    assert result["requested_language"] == "japanese"
    assert result["language_applied"] is False


def test_launch_status_reports_an_external_games_storage(tmp_path: Path, monkeypatch) -> None:
    project = _make_project(tmp_path)
    monkeypatch.setattr(
        live.BridgeClient,
        "from_project",
        classmethod(lambda cls, root, **_kwargs: _InfoClient(None)),
    )

    status = live.launch_status(str(project))

    assert status["external"] is True
    assert status["isolation"] == {"exposes_user_saves": True}


@pytest.mark.parametrize(
    ("savedir", "field"),
    [("existing", "persistent"), ("existing", "preferences")],
)
def test_reuse_cannot_accept_state_modes_a_fresh_launch_rejects(
    tmp_path: Path, monkeypatch, savedir: str, field: str
) -> None:
    result = _external_reuse(
        tmp_path, monkeypatch, _InfoClient(None), savedir=savedir, **{field: "empty"}
    )

    assert result["ok"] is False
    assert result["code"] == "LAUNCH_STATE_MODE_CONFLICT"
    assert "savedir='temporary'" in result["error"]


def test_a_default_launch_still_reuses_any_external_game(tmp_path: Path, monkeypatch) -> None:
    # Without isolation the launch uses the player's saves anyway, so there is
    # nothing to protect, and an older bridge stays reusable.
    result = _external_reuse(tmp_path, monkeypatch, _InfoClient(None))

    assert result["ok"] is True
    assert result["external"] is True


def test_launch_reuses_owned_session_only_when_editor_mode_matches(tmp_path: Path) -> None:
    project = _make_project(tmp_path)
    key = live._key(project)
    session = _OwnedRunningSession(editor=True, isolation=_isolation())
    live._SESSIONS[key] = session

    mismatch = live.launch_game(str(project), editor=False)

    assert mismatch["ok"] is False
    assert mismatch["code"] == "SESSION_MODE_MISMATCH"
    assert mismatch["requested_editor"] is False
    assert mismatch["existing_editor"] is True
    assert live._SESSIONS[key] is session
    assert session.close_calls == 0

    matched = live.launch_game(str(project), editor=True)
    assert {k: matched[k] for k in (
        "ok", "already_running", "ready", "current_label", "editor",
        "requested_language", "active_language",
    )} == {
        "ok": True,
        "already_running": True,
        "ready": True,
        "current_label": "dashboard_scene",
        "editor": True,
        "requested_language": None,
        "active_language": None,
    }
    assert matched["isolation"]["exposes_user_saves"] is False
    assert live._SESSIONS[key] is session
    assert session.close_calls == 0
    live._SESSIONS.pop(key, None)


def test_an_isolated_launch_never_reuses_a_session_on_the_players_saves(tmp_path: Path) -> None:
    """An isolated launch is mutating; handing back a savedir=existing session would skip authorize."""
    project = _make_project(tmp_path)
    key = live._key(project)
    session = _OwnedRunningSession(editor=True, isolation=_isolation("existing"))
    live._SESSIONS[key] = session
    try:
        refused = live.launch_game(str(project), savedir="temporary")
        assert refused["ok"] is False
        assert refused["code"] == "SESSION_ISOLATION_MISMATCH"
        assert session.close_calls == 0
        assert live.launch_game(str(project), savedir="existing")["already_running"] is True
    finally:
        live._SESSIONS.pop(key, None)


def test_a_play_only_server_never_reuses_a_bridge_that_runs_code(tmp_path: Path, monkeypatch) -> None:
    project = _make_project(tmp_path)
    key = live._key(project)
    monkeypatch.setenv("RENFORGE_TOOLSETS", "play")
    live._SESSIONS[key] = _OwnedRunningSession(editor=True, isolation=_isolation(), bridge_eval=True)
    try:
        refused = live.launch_game(str(project))
        assert refused["code"] == "SESSION_EVAL_MISMATCH"
        live._SESSIONS[key] = _OwnedRunningSession(editor=True, isolation=_isolation(), bridge_eval=False)
        assert live.launch_game(str(project))["already_running"] is True
    finally:
        live._SESSIONS.pop(key, None)


def test_an_isolated_launch_never_reuses_a_session_authorized_onto_a_foreign_folder(tmp_path: Path) -> None:
    """savedir=<another app's folder> needed authorize; an isolated launch must not inherit it.

    The per-component rule alone would allow it: that folder is not the player's.
    """
    project = _make_project(tmp_path)
    key = live._key(project)
    foreign = tmp_path / "Documents" / "game-saves"
    live._SESSIONS[key] = _OwnedRunningSession(editor=True, isolation=_isolation("path", savedir=foreign))
    try:
        assert live.launch_game(str(project), savedir="temporary")["code"] == "SESSION_ISOLATION_MISMATCH"
        assert live.launch_game(str(project), savedir=str(foreign))["already_running"] is True
    finally:
        live._SESSIONS.pop(key, None)


def test_a_launch_that_arrives_while_another_starts_must_match_it(tmp_path: Path, monkeypatch) -> None:
    """LAUNCH_IN_PROGRESS would point the caller at a session it may not be given."""
    project = _make_project(tmp_path)
    release = threading.Event()

    def slow_launch(_root, _cancel):
        release.wait(5)
        return {"ok": False, "ready": False, "error": "stopped by the test"}

    everything = frozenset({"savedir", "home"})
    exposed = {"exposed": everything, "isolated": False, "eval": True}
    try:
        live.start_launch(str(project), slow_launch, wait_timeout=0, requirements=exposed)
        refused = live.start_launch(str(project), slow_launch, wait_timeout=0,
                                    requirements={"exposed": frozenset(), "isolated": True, "eval": True})
        assert refused["code"] == "SESSION_ISOLATION_MISMATCH"
        refused = live.start_launch(str(project), slow_launch, wait_timeout=0,
                                    requirements={"exposed": everything, "isolated": False, "eval": False})
        assert refused["code"] == "SESSION_EVAL_MISMATCH"
        same = live.start_launch(str(project), slow_launch, wait_timeout=0, requirements=exposed)
        assert same["code"] == "LAUNCH_IN_PROGRESS"
    finally:
        release.set()
        for _ in range(50):
            if not live._LAUNCHES or all(task.done_event.is_set() for task in live._LAUNCHES.values()):
                break
            time.sleep(0.05)
        live._LAUNCHES.pop(live._key(project), None)


@pytest.mark.parametrize("info, toolsets, code", [
    # An older bridge cannot say: assume the worst.
    (None, "", "SESSION_ISOLATION_MISMATCH"),
    ({"eval_enabled": False, "savedir": None, "home": "SESSION"}, "", "SESSION_ISOLATION_MISMATCH"),
    ({"eval_enabled": True, "savedir": "HOST", "home": "SESSION"}, "", "SESSION_ISOLATION_MISMATCH"),
    # Saves isolated but HOME the player's own (home=existing): not isolated.
    ({"eval_enabled": True, "savedir": "SESSION", "home": "PLAYER"}, "", "SESSION_ISOLATION_MISMATCH"),
    ({"eval_enabled": True, "savedir": "SESSION", "home": None}, "", "SESSION_ISOLATION_MISMATCH"),
    ({"eval_enabled": True, "savedir": "SESSION", "home": "SESSION"}, "play", "SESSION_EVAL_MISMATCH"),
    ({"eval_enabled": False, "savedir": "SESSION", "home": "SESSION"}, "play", None),
    ({"eval_enabled": True, "savedir": "SESSION", "home": "SESSION"}, "", None),
])
def test_an_external_session_is_reused_only_when_it_matches(
    tmp_path: Path, monkeypatch, info, toolsets, code
) -> None:
    from renforge.save_isolation import host_renpy_root

    project = _make_project(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("APPDATA", str(tmp_path / "home" / "AppData" / "Roaming"))
    monkeypatch.setenv("RENFORGE_TOOLSETS", toolsets)
    if info is not None:
        def where(name, leaf):
            return {"HOST": str(host_renpy_root() / "demo"), "PLAYER": str(tmp_path / "home"),
                    "SESSION": str(_SESSION_ROOT / leaf), None: None}[name]

        info = dict(info, savedir=where(info["savedir"], "saves"), home=where(info["home"], "home"))
        if info["savedir"]:
            info["save_locations"] = [info["savedir"]]
    monkeypatch.setattr(live.BridgeClient, "from_project",
                        classmethod(lambda cls, root, **_kwargs: _InfoClient(info)))
    # Only an isolated launch can be refused a game for its storage.
    result = live.launch_game(str(project), editor=False, savedir="temporary")

    if code is None:
        assert result["already_running"] is True and result["external"] is True
    else:
        assert result["ok"] is False and result["code"] == code


def test_an_isolated_launch_is_not_handed_a_game_using_the_players_saves(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.delenv("RENFORGE_ISOLATION", raising=False)
    project = _make_project(tmp_path)
    key = live._key(project)
    session = _OwnedRunningSession(
        editor=True, isolation=_isolation(savedir_mode="existing", home_mode="existing")
    )
    live._SESSIONS[key] = session
    try:
        refused = live.launch_game(str(project), editor=True, savedir="temporary")
        reused = live.launch_game(str(project), editor=True)
    finally:
        live._SESSIONS.pop(key, None)

    assert refused["ok"] is False
    assert refused["code"] == "SESSION_ISOLATION_MISMATCH"
    assert refused["existing_exposes_user_saves"] is True
    # A launch that asked for nothing gets the running game, as before.
    assert reused["already_running"] is True
    assert session.close_calls == 0
    assert session.close_calls == 0


@pytest.mark.parametrize(
    ("request_knobs", "running", "reused"),
    [
        # Isolated saves with the host HOME must not get a game that writes
        # its saves into the player's save folder, and the reverse.
        ({"savedir": "temporary", "home": "existing"}, ("existing", "existing"), False),
        ({"savedir": "temporary", "home": "existing"}, ("existing", "temporary"), False),
        ({"savedir": "existing", "home": "temporary"}, ("existing", "existing"), False),
        ({"savedir": "existing", "home": "temporary"}, ("temporary", "existing"), False),
        # A game isolating every component the launch asked for is reusable,
        # and one that isolates more is harmless.
        ({"savedir": "temporary", "home": "existing"}, ("temporary", "existing"), True),
        ({"savedir": "temporary", "home": "existing"}, ("temporary", "temporary"), True),
        ({"savedir": "existing", "home": "temporary"}, ("existing", "temporary"), True),
        ({"savedir": "temporary"}, ("temporary", "temporary"), True),
        # A launch that isolates nothing reuses anything.
        ({}, ("existing", "existing"), True),
        ({}, ("temporary", "existing"), True),
        ({}, ("temporary", "temporary"), True),
    ],
)
def test_reusing_an_owned_game_checks_saves_and_home_separately(
    tmp_path: Path, monkeypatch, request_knobs, running, reused
) -> None:
    monkeypatch.delenv("RENFORGE_ISOLATION", raising=False)
    project = _make_project(tmp_path)
    key = live._key(project)
    savedir_mode, home_mode = running
    session = _OwnedRunningSession(
        editor=True, isolation=_isolation(savedir_mode=savedir_mode, home_mode=home_mode)
    )
    live._SESSIONS[key] = session
    try:
        result = live.launch_game(str(project), editor=True, **request_knobs)
    finally:
        live._SESSIONS.pop(key, None)

    if reused:
        assert result["ok"] is True
        assert result["already_running"] is True
    else:
        assert result["ok"] is False
        assert result["code"] == "SESSION_ISOLATION_MISMATCH"
        assert result["existing_exposes_user_saves"] is True
        # Each refused request leaves one component on the player's storage.
        assert result["requested_exposes_user_saves"] is True
    assert session.close_calls == 0


@pytest.mark.parametrize(
    ("request_knobs", "info", "reused"),
    [
        ({"savedir": "temporary", "home": "existing"},
         {"savedir": "HOST_SAVES", "home": "SESSION_HOME", "save_locations": ["HOST_SAVES"]}, False),
        ({"savedir": "temporary", "home": "existing"},
         {"savedir": "HOST_SAVES", "home": "HOST_HOME", "save_locations": ["HOST_SAVES"]}, False),
        ({"savedir": "existing", "home": "temporary"},
         {"savedir": "SESSION_SAVES", "home": "HOST_HOME", "save_locations": ["SESSION_SAVES"]}, False),
        # An older bridge cannot tell where it keeps either component.
        ({"savedir": "temporary", "home": "existing"}, None, False),
        ({"savedir": "existing", "home": "temporary"}, None, False),
        ({"savedir": "temporary", "home": "existing"},
         {"savedir": "SESSION_SAVES", "home": "HOST_HOME", "save_locations": ["SESSION_SAVES"]}, True),
        ({"savedir": "temporary", "home": "existing"},
         {"savedir": "SESSION_SAVES", "home": "SESSION_HOME", "save_locations": ["SESSION_SAVES"]}, True),
        ({"savedir": "existing", "home": "temporary"},
         {"savedir": "HOST_SAVES", "home": "SESSION_HOME", "save_locations": ["HOST_SAVES"]}, True),
    ],
)
def test_reusing_an_external_game_checks_saves_and_home_separately(
    tmp_path: Path, monkeypatch, request_knobs, info, reused
) -> None:
    host_home = _host_home(tmp_path, monkeypatch)
    host_saves = host_renpy_root() / "demo"
    session = tmp_path / "renforge-session-x"
    for path in (host_saves, session / "saves", session / "home"):
        path.mkdir(parents=True)
    if info is not None:
        mapping = {
            "SESSION_SAVES": str(session / "saves"),
            "SESSION_HOME": str(session / "home"),
            "HOST_HOME": str(host_home),
            "HOST_SAVES": str(host_saves),
        }
        info = {
            key: [mapping[item] for item in value] if isinstance(value, list) else mapping[value]
            for key, value in info.items()
        }

    result = _external_reuse(tmp_path, monkeypatch, _InfoClient(info), **request_knobs)

    if reused:
        assert result["ok"] is True
        assert result["external"] is True
    else:
        assert result["ok"] is False
        assert result["code"] == "SESSION_ISOLATION_MISMATCH"
        assert result["existing_exposes_user_saves"] is True


def _linked_home(tmp_path: Path, host_home: Path) -> Path:
    """A HOME whose Ren'Py save folder is a link into the host save tree."""
    linked = tmp_path / "linked-home"
    implied = linked / host_renpy_root().relative_to(host_home)
    implied.parent.mkdir(parents=True, exist_ok=True)
    host_renpy_root().mkdir(parents=True, exist_ok=True)
    try:
        implied.symlink_to(host_renpy_root(), target_is_directory=True)
    except OSError:
        pytest.skip("symlinks are not available")
    return linked


def _request_paths(tmp_path: Path, host_home: Path, knobs: dict) -> dict:
    """Swap placeholder knob values for paths the wider "might be the player's" rule counts."""
    other_case = host_renpy_root() / "demo"
    other_case = host_home / str(other_case.relative_to(host_home)).swapcase()
    paths = {
        # Another game's own save folder: not this project's saves.
        "OTHER_GAME_SAVES": str(tmp_path / "scratch" / "game" / "saves"),
        # The host save tree spelled in another case.
        "HOST_TREE_OTHER_CASE": str(other_case),
    }
    resolved = {key: paths.get(value, value) for key, value in knobs.items()}
    if resolved.get("home") == "LINKED_HOME":
        resolved["home"] = str(_linked_home(tmp_path, host_home))
    return resolved


# Requests whose savedir or HOME only might be the player's: they must not be
# handed a running game that certainly uses the player's storage.
_MIGHT_BE_THE_PLAYERS = [
    ({"savedir": "OTHER_GAME_SAVES"}, ("existing", "existing")),
    ({"savedir": "HOST_TREE_OTHER_CASE"}, ("existing", "existing")),
    ({"savedir": "temporary", "home": "LINKED_HOME"}, ("temporary", "existing")),
]


@pytest.mark.parametrize(("request_knobs", "running"), _MIGHT_BE_THE_PLAYERS)
def test_a_request_that_might_reach_the_players_saves_is_not_handed_a_game_that_does(
    tmp_path: Path, monkeypatch, request_knobs, running
) -> None:
    host_home = _host_home(tmp_path, monkeypatch)
    project = _make_project(tmp_path)
    key = live._key(project)
    savedir_mode, home_mode = running
    session = _OwnedRunningSession(editor=False, isolation=_isolation(savedir_mode, home_mode=home_mode))
    live._SESSIONS[key] = session
    try:
        result = live.launch_game(str(project), editor=False, **_request_paths(tmp_path, host_home, request_knobs))
    finally:
        live._SESSIONS.pop(key, None)

    assert result["ok"] is False
    assert result["code"] == "SESSION_ISOLATION_MISMATCH"
    assert session.close_calls == 0


@pytest.mark.parametrize(("request_knobs", "running"), _MIGHT_BE_THE_PLAYERS)
def test_a_request_that_might_reach_the_players_saves_is_not_handed_an_external_game_that_does(
    tmp_path: Path, monkeypatch, request_knobs, running
) -> None:
    host_home = _host_home(tmp_path, monkeypatch)
    host_saves = host_renpy_root() / "demo"
    session = tmp_path / "renforge-session-x"
    for path in (host_saves, session / "saves", session / "home"):
        path.mkdir(parents=True, exist_ok=True)
    savedir = host_saves if running[0] == "existing" else session / "saves"
    home = host_home if running[1] == "existing" else session / "home"
    info = {"eval_enabled": True, "savedir": str(savedir), "home": str(home), "save_locations": [str(savedir)]}

    result = _external_reuse(tmp_path, monkeypatch, _InfoClient(info),
                             **_request_paths(tmp_path, host_home, request_knobs))

    assert result["ok"] is False
    assert result["code"] == "SESSION_ISOLATION_MISMATCH"


@pytest.mark.parametrize(
    "request_knobs",
    [
        # What renforge_session launch asks for.
        {"savedir": "STATE_SAVES", "home": "temporary"},
        {"savedir": "temporary"},
        {},
    ],
)
def test_an_external_game_in_renforge_folders_is_not_the_players(tmp_path: Path, monkeypatch, request_knobs) -> None:
    """A renforge_session game started by another process keeps its saves in the state folder."""
    _host_home(tmp_path, monkeypatch)
    project = tmp_path / "project"
    state_saves = project_state_dir(project) / "saves"
    session_home = tmp_path / "renforge-session-x" / "home"
    for path in (state_saves, session_home):
        path.mkdir(parents=True)
    info = {"eval_enabled": True, "savedir": str(state_saves), "home": str(session_home),
            "save_locations": [str(state_saves)]}
    knobs = {key: str(state_saves) if value == "STATE_SAVES" else value for key, value in request_knobs.items()}

    result = _external_reuse(tmp_path, monkeypatch, _InfoClient(info), **knobs)
    status = live.launch_status(str(project))

    assert result["ok"] is True and result["external"] is True
    assert result["isolation"]["exposes_user_saves"] is False
    assert status["isolated"] is True
    assert status["isolation"]["exposes_user_saves"] is False


def test_an_external_game_is_the_players_when_a_renforge_folder_links_into_their_saves(
    tmp_path: Path, monkeypatch
) -> None:
    """A state-folder location that resolves into the host save tree is still the player's."""
    _host_home(tmp_path, monkeypatch)
    project = tmp_path / "project"
    host_saves = host_renpy_root() / "demo"
    host_saves.mkdir(parents=True)
    state_saves = project_state_dir(project) / "saves"
    state_saves.parent.mkdir(parents=True)
    try:
        state_saves.symlink_to(host_saves, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks are not available")
    session_home = tmp_path / "renforge-session-x" / "home"
    session_home.mkdir(parents=True)
    info = {"eval_enabled": True, "savedir": str(state_saves), "home": str(session_home),
            "save_locations": [str(state_saves)]}

    result = _external_reuse(tmp_path, monkeypatch, _InfoClient(info), savedir="temporary")

    assert result["code"] == "SESSION_ISOLATION_MISMATCH"


def test_launch_rejects_editor_mode_for_unproven_external_bridge(tmp_path: Path, monkeypatch) -> None:
    project = _make_project(tmp_path)
    monkeypatch.setattr(
        live.BridgeClient,
        "from_project",
        classmethod(lambda cls, root, **_kwargs: _StateClient()),
    )
    monkeypatch.setattr(
        live,
        "get_or_install_sdk",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("must not relaunch")),
    )

    result = live.launch_game(str(project), editor=True)

    assert result["ok"] is False
    assert result["ready"] is False
    assert result["code"] == "SESSION_MODE_MISMATCH"
    assert result["requested_editor"] is True
    assert result["existing_editor"] is False
    assert "cannot be proven for an external session" in result["message"]


def test_new_launch_passes_editor_mode_to_bridge_launcher(tmp_path: Path, monkeypatch) -> None:
    project = _make_project(tmp_path)
    launch_kwargs: dict[str, object] = {}
    monkeypatch.setattr(live, "get_or_install_sdk", lambda *_args, **_kwargs: "sdk")

    def fake_launch_with_bridge(_sdk, _project, **kwargs):
        launch_kwargs.update(kwargs)
        return _LaunchedSession(editor=bool(kwargs.get("editor")))

    monkeypatch.setattr(live, "launch_with_bridge", fake_launch_with_bridge)

    result = live.launch_game(str(project), editor=True)

    assert result["ok"] is True
    assert result["editor"] is True
    assert launch_kwargs["editor"] is True
    # Isolation is opt-in: a plain launch keeps the player's saves and HOME.
    assert launch_kwargs["savedir"] == "existing"
    assert launch_kwargs["home"] == "existing"
    assert launch_kwargs["persistent"] == "existing"
    assert launch_kwargs["preferences"] == "existing"

    live._SESSIONS.pop(live._key(project), None)
    launch_kwargs.clear()

    result = live.launch_game(str(project), editor=False)
    assert result["ok"] is True
    assert result["editor"] is False
    assert launch_kwargs["editor"] is False

    live._SESSIONS.pop(live._key(project), None)


def test_launch_status_preserves_editor_for_owned_session(tmp_path: Path) -> None:
    project = _make_project(tmp_path)
    key = live._key(project)
    session = _OwnedRunningSession(editor=True)
    live._SESSIONS[key] = session

    result = live.launch_status(str(project))

    assert {k: result[k] for k in (
        "ok", "ready", "status", "current_label", "editor", "active_language",
    )} == {
        "ok": True,
        "ready": True,
        "status": "ready",
        "current_label": "dashboard_scene",
        "editor": True,
        "active_language": None,
    }
    assert result["isolation"]["exposes_user_saves"] is False
    # Its saves and HOME are in a session root, and its bridge may run code.
    assert result["isolated"] is True
    assert result["eval_enabled"] is True
    live._SESSIONS.pop(key, None)


def test_launch_status_of_an_owned_session_without_an_isolation_record_assumes_the_worst(tmp_path: Path) -> None:
    project = _make_project(tmp_path)
    key = live._key(project)
    session = _OwnedRunningSession(editor=True)
    session.isolation = None
    live._SESSIONS[key] = session
    try:
        result = live.launch_status(str(project))
        refused = live.launch_game(str(project), editor=True, savedir="temporary")
    finally:
        live._SESSIONS.pop(key, None)

    assert result["ready"] is True
    assert result["isolated"] is False
    assert refused["code"] == "SESSION_ISOLATION_MISMATCH"
    assert refused["existing_exposes_user_saves"] is True


def test_warp_refuses_to_stop_a_live_external_bridge(tmp_path: Path, monkeypatch) -> None:
    project = _make_project(tmp_path)
    monkeypatch.setattr(
        live.BridgeClient,
        "from_project",
        classmethod(lambda cls, root, **_kwargs: _StateClient()),
    )
    monkeypatch.setattr(
        live,
        "stop_external_game",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("must not stop another bridge owner")
        ),
    )
    monkeypatch.setattr(
        live,
        "get_or_install_sdk",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("must not resolve an SDK while the project is locked")
        ),
    )

    result = live.launch_game(str(project), warp="chapter_one")

    assert result["ok"] is False
    assert result["ready"] is False
    assert result["code"] == "BRIDGE_PROJECT_LOCKED"
    assert result["phase"] == "acquiring_project_lock"
    assert "owning RenForge session" in result["message"]
    assert result["error"] == result["message"]


@pytest.mark.parametrize("bundled,selector", [
    ("7.0.0.18090907", "stable"), ("7.8.7", "8.5.3"),
    (None, "7.8.7"), (None, "6.99.14"),
])
def test_legacy_runtime_rejected_before_sdk_resolution_or_injection(tmp_path, monkeypatch, bundled, selector):
    project = _make_project(tmp_path)
    if bundled:
        (project / "renpy").mkdir()
        (project / "renpy" / "vc_version.py").write_text(f"version = {bundled!r}\n")
    monkeypatch.setattr(live, "get_or_install_sdk", lambda *args, **kwargs: pytest.fail("must not resolve/download SDK"))
    monkeypatch.setattr(live, "launch_with_bridge", lambda *args, **kwargs: pytest.fail("must not inject/launch"))
    result = live.launch_game(str(project), version=selector)
    assert result["code"] == "UNSUPPORTED_RENPY_VERSION", result
    assert result["phase"] == "checking_runtime_compatibility"
    assert result["details"]["minimum_version"] == "8.0.0"
    assert not (control_dir(project)).exists()
    assert sorted(path.name for path in (project / "game").iterdir()) == ["script.rpy"]


def test_legacy_stable_configuration_is_rejected_before_download(tmp_path, monkeypatch):
    project = _make_project(tmp_path)
    monkeypatch.setenv("RENPY_SDK_STABLE_VERSION", "7.8.7")
    monkeypatch.setattr(live, "get_or_install_sdk", lambda *args, **kwargs: pytest.fail("must not download SDK"))
    result = live.launch_game(str(project))
    assert result["code"] == "UNSUPPORTED_RENPY_VERSION", result
    assert result["details"]["source"] == "selected_sdk"


def test_new_launch_resolves_sdk_for_exact_project_root(tmp_path: Path, monkeypatch) -> None:
    project = _make_project(tmp_path)
    project_path = project / ".." / project.name
    resolved: dict[str, object] = {}

    def fake_resolve(version: str, *, project_root: Path):
        resolved["version"] = version
        resolved["project_root"] = project_root
        return "sdk"

    monkeypatch.setattr(live, "get_or_install_sdk", fake_resolve)
    monkeypatch.setattr(
        live,
        "launch_with_bridge",
        lambda sdk, renpy_project, **_kwargs: _LaunchedSession(),
    )

    result = live.launch_game(str(project_path), version="8.5.3")

    assert result["ok"] is True
    assert resolved == {
        "version": "8.5.3",
        "project_root": project.resolve(),
    }
    live._SESSIONS.pop(live._key(project), None)


def test_warp_retries_incomplete_existing_session_teardown(
    tmp_path: Path,
    monkeypatch,
) -> None:
    project = _make_project(tmp_path)
    key = live._key(project)
    session = _RetryingSession()
    live._SESSIONS[key] = session
    monkeypatch.setattr(
        live,
        "get_or_install_sdk",
        lambda *_args, **_kwargs: "sdk",
    )
    monkeypatch.setattr(
        live,
        "launch_with_bridge",
        lambda *_args, **_kwargs: _LaunchedSession(),
    )

    first = live.launch_game(str(project), warp="chapter_one")

    assert first["ok"] is False
    assert first["code"] == "BRIDGE_TEARDOWN_INCOMPLETE"
    assert first["phase"] == "stopping_existing_session"
    assert first["failed"] == ["process_alive"]
    assert live._SESSIONS[key] is session
    session.process_exited = True

    second = live.launch_game(str(project), warp="chapter_one")

    assert second["ok"] is True
    assert session.close_calls == 2
    assert live._SESSIONS[key] is not session
    live._SESSIONS.pop(key, None)


def test_warp_keeps_existing_session_when_teardown_raises(tmp_path: Path) -> None:
    project = _make_project(tmp_path)
    key = live._key(project)
    session = _RaisingSession()
    live._SESSIONS[key] = session

    result = live.launch_game(str(project), warp="chapter_one")

    assert result["ok"] is False
    assert result["code"] == "BRIDGE_TEARDOWN_INCOMPLETE"
    assert result["phase"] == "stopping_existing_session"
    assert result["failed"] == ["close"]
    assert "RuntimeError: teardown failed" in result["error"]
    assert live._SESSIONS[key] is session
    live._SESSIONS.pop(key, None)


def test_start_launch_returns_before_slow_startup_and_exposes_ready_status(tmp_path: Path) -> None:
    project = _make_project(tmp_path)
    release = threading.Event()
    started = threading.Event()

    def delayed_launch(project_root: Path, _cancel_event: threading.Event) -> dict:
        assert project_root == project.resolve()
        started.set()
        assert release.wait(2.0)
        return {"ok": True, "ready": True, "current_label": "main_menu"}

    result = live.start_launch(str(project), delayed_launch, wait_timeout=0.0)

    assert started.wait(1.0)
    assert result["ok"] is True
    assert result["ready"] is False
    assert result["status"] == "starting"

    ignored_launch = threading.Event()
    conflict = live.start_launch(
        str(project),
        lambda _project_root, _cancel_event: ignored_launch.set() or {"ok": True, "ready": True},
        wait_timeout=0.0,
    )
    assert conflict["ok"] is False
    assert conflict["code"] == "LAUNCH_IN_PROGRESS"
    assert conflict["status"] == "starting"
    assert ignored_launch.is_set() is False

    release.set()
    assert _wait_for_launch_status(project, "ready")["ready"] is True
    live.stop_game(str(project))


def test_start_launch_defaults_editor_true(tmp_path: Path) -> None:
    project = _make_project(tmp_path)
    release = threading.Event()
    started = threading.Event()

    def delayed_launch(_project_root: Path, _cancel_event: threading.Event) -> dict:
        started.set()
        assert release.wait(2.0)
        return {"ok": True, "ready": True, "current_label": "main_menu"}

    result = live.start_launch(str(project), delayed_launch, wait_timeout=0.0)

    assert started.wait(1.0)
    assert result["ok"] is True
    assert result["ready"] is False
    assert result["status"] == "starting"
    assert result["editor"] is True

    release.set()
    final = _wait_for_launch_status(project, "ready")
    assert final["ok"] is True
    assert final["ready"] is True
    assert final["editor"] is True
    live.stop_game(str(project))


def test_start_launch_exposes_requested_editor_true_in_status_and_result(tmp_path: Path) -> None:
    project = _make_project(tmp_path)
    release = threading.Event()
    started = threading.Event()

    def delayed_launch(_project_root: Path, _cancel_event: threading.Event) -> dict:
        started.set()
        assert release.wait(2.0)
        return {"ok": True, "ready": True, "current_label": "main_menu"}

    result = live.start_launch(
        str(project),
        delayed_launch,
        editor=True,
        wait_timeout=0.0,
    )

    assert started.wait(1.0)
    assert result["ok"] is True
    assert result["ready"] is False
    assert result["status"] == "starting"
    assert result["editor"] is True

    release.set()
    final = _wait_for_launch_status(project, "ready")
    assert final["ok"] is True
    assert final["ready"] is True
    assert final["editor"] is True
    live.stop_game(str(project))


def test_launch_status_exposes_a_failed_startup(tmp_path: Path) -> None:
    project = _make_project(tmp_path)

    result = live.start_launch(
        str(project),
        lambda _project_root, _cancel_event: {
            "ok": False,
            "code": "RENPY_PROCESS_EXITED",
            "error": "Game exited during startup.",
        },
        wait_timeout=1.0,
    )

    assert result["ok"] is False
    assert result["ready"] is False
    assert result["status"] == "failed"
    live.stop_game(str(project))


def test_stop_game_cancels_a_pending_launch(tmp_path: Path) -> None:
    project = _make_project(tmp_path)
    started = threading.Event()

    def cancellable_launch(_project_root: Path, cancel_event: threading.Event) -> dict:
        started.set()
        assert cancel_event.wait(2.0)
        return live.cancelled_launch_result()

    result = live.start_launch(str(project), cancellable_launch, wait_timeout=0.0)

    assert started.wait(1.0)
    assert result["status"] == "starting"
    assert live.stop_game(str(project)) == {
        "ok": True,
        "was_running": True,
        "launch_cancelled": True,
    }
    idle = live.launch_status(str(project))
    assert idle["ok"] is True
    assert idle["status"] == "idle"


def test_stop_game_keeps_a_partial_session_registered_for_retry(tmp_path: Path) -> None:
    project = _make_project(tmp_path)
    key = live._key(project)
    session = _RetryingSession()
    live._SESSIONS[key] = session

    first = live.stop_game(str(project))

    assert first["failed"] == ["process_alive"]
    assert live._SESSIONS[key] is session
    session.process_exited = True

    second = live.stop_game(str(project))

    assert second["failed"] == []
    assert session.close_calls == 2
    assert key not in live._SESSIONS


def test_stop_all_keeps_partial_sessions_registered_for_retry(tmp_path: Path) -> None:
    partial_key = live._key(tmp_path / "partial")
    closed_key = live._key(tmp_path / "closed")
    partial = _RetryingSession()
    closed = _RetryingSession(process_exited=True)
    live._SESSIONS[partial_key] = partial
    live._SESSIONS[closed_key] = closed

    live.stop_all()

    assert live._SESSIONS[partial_key] is partial
    assert closed_key not in live._SESSIONS
    partial.process_exited = True
    live.stop_all()
    assert partial_key not in live._SESSIONS


def test_stop_game_attempts_external_stop_when_cancellation_is_still_pending(
    tmp_path: Path,
    monkeypatch,
) -> None:
    project = _make_project(tmp_path)
    release = threading.Event()
    started = threading.Event()

    def slow_cancel(_project_root: Path, _cancel_event: threading.Event) -> dict:
        started.set()
        assert release.wait(2.0)
        return live.cancelled_launch_result()

    live.start_launch(str(project), slow_cancel, wait_timeout=0.0)
    assert started.wait(1.0)
    monkeypatch.setattr(live, "_LAUNCH_CANCEL_WAIT_SECONDS", 0.0)
    monkeypatch.setattr(
        live,
        "stop_external_game",
        lambda _project_path: {"ok": True, "was_running": True},
    )

    result = live.stop_game(str(project))

    assert result["ok"] is True
    assert result["was_running"] is True
    assert result["launch_cancel_requested"] is True
    assert result["external_stopped"] is True
    pending = live.launch_status(str(project))
    assert pending["status"] == "starting"
    assert pending["cancel_requested"] is True
    release.set()
    assert _wait_for_launch_status(project, "failed")["code"] == "LAUNCH_CANCELLED"
    live.stop_game(str(project))


def test_stop_game_reports_an_incomplete_teardown_as_not_ok(tmp_path: Path) -> None:
    project = _make_project(tmp_path)
    key = live._key(project)
    live._SESSIONS[key] = _RetryingSession()
    try:
        result = live.stop_game(str(project))
    finally:
        live._SESSIONS.pop(key, None)

    assert result["ok"] is False
    assert result["code"] == "BRIDGE_TEARDOWN_INCOMPLETE"
    assert result["failed"] == ["process_alive"]
    assert "process_alive" in result["error"]


def test_stop_game_reports_the_close_error_itself(tmp_path: Path) -> None:
    project = _make_project(tmp_path)
    key = live._key(project)

    class _Exploding(_OwnedRunningSession):
        def close(self) -> dict:
            raise OSError("bridge socket gone")

    session = _Exploding()
    task = live._LaunchTask(project)
    task.lifecycle = "ready"
    task.session = session
    live._LAUNCHES[key] = task
    try:
        # The close error propagates unchanged, not as an error from the cleanup code.
        with pytest.raises(OSError, match="bridge socket gone"):
            live.stop_game(str(project))
    finally:
        live._LAUNCHES.pop(key, None)
        live._SESSIONS.pop(key, None)


def _relaunch_knobs(
    tmp_path: Path, monkeypatch, existing_isolation, *, isolation_env: str | None = None, **launch_kwargs
) -> dict:
    """Replace an owned session with a warp launch; return the knobs it launched with."""
    monkeypatch.delenv("RENFORGE_ISOLATION", raising=False)
    if isolation_env is not None:
        monkeypatch.setenv("RENFORGE_ISOLATION", isolation_env)
    project = _make_project(tmp_path)
    key = live._key(project)
    live._SESSIONS[key] = _OwnedRunningSession(editor=True, isolation=existing_isolation)
    captured: dict = {}

    def fake_launch(_sdk, _project, **kwargs):
        captured.update({k: kwargs.get(k) for k in ("savedir", "home", "persistent", "preferences")})
        return _LaunchedSession(editor=True)

    monkeypatch.setattr(live, "get_or_install_sdk", lambda *_a, **_k: "sdk")
    monkeypatch.setattr(live, "launch_with_bridge", fake_launch)
    try:
        result = live.launch_game(str(project), warp="game/script.rpy:1", **launch_kwargs)
    finally:
        live._SESSIONS.pop(key, None)
    assert result["ok"] is True
    return captured


def test_a_warp_relaunch_of_an_isolated_session_stays_isolated(tmp_path: Path, monkeypatch) -> None:
    # Maintainer panel I2: jump/new_game/Warp replaced an isolated game with
    # one on the player's saves, persistent data and HOME, silently.
    knobs = _relaunch_knobs(tmp_path, monkeypatch, _isolation(savedir_mode="temporary"))
    assert knobs == {
        "savedir": "temporary",
        "home": "temporary",
        "persistent": "empty",
        "preferences": "empty",
    }


def test_a_warp_relaunch_of_a_non_isolated_session_stays_on_the_players_saves(
    tmp_path: Path, monkeypatch
) -> None:
    knobs = _relaunch_knobs(
        tmp_path, monkeypatch, _isolation(savedir_mode="existing", home_mode="existing")
    )
    assert knobs == {
        "savedir": "existing",
        "home": "existing",
        "persistent": "existing",
        "preferences": "existing",
    }


@pytest.mark.parametrize(
    ("running", "expected"),
    [
        # Isolated saves with the host HOME: the saves must stay isolated.
        (("temporary", "existing"),
         {"savedir": "temporary", "home": "existing", "persistent": "empty", "preferences": "empty"}),
        # The player's saves with a private HOME: HOME must stay private.
        (("existing", "temporary"),
         {"savedir": "existing", "home": "temporary", "persistent": "existing", "preferences": "existing"}),
    ],
)
def test_a_warp_relaunch_of_a_partly_isolated_session_keeps_each_component(
    tmp_path: Path, monkeypatch, running, expected
) -> None:
    savedir_mode, home_mode = running
    knobs = _relaunch_knobs(
        tmp_path, monkeypatch, _isolation(savedir_mode=savedir_mode, home_mode=home_mode)
    )
    assert knobs == expected


@pytest.mark.parametrize(
    "running", [("temporary", "existing"), ("existing", "temporary"), ("existing", "existing")]
)
def test_a_warp_relaunch_never_drops_isolation_the_server_configures(
    tmp_path: Path, monkeypatch, running
) -> None:
    # With RENFORGE_ISOLATION=temporary an all-auto relaunch may keep or add
    # isolation, never fall back to the player's saves or HOME.
    savedir_mode, home_mode = running
    knobs = _relaunch_knobs(
        tmp_path,
        monkeypatch,
        _isolation(savedir_mode=savedir_mode, home_mode=home_mode),
        isolation_env="temporary",
    )
    assert knobs == {
        "savedir": "temporary",
        "home": "temporary",
        "persistent": "empty",
        "preferences": "empty",
    }


def test_an_explicit_savedir_on_a_warp_relaunch_is_honoured(tmp_path: Path, monkeypatch) -> None:
    knobs = _relaunch_knobs(
        tmp_path, monkeypatch, _isolation(savedir_mode="temporary"), savedir="existing"
    )
    assert knobs["savedir"] == "existing"
    assert knobs["home"] == "existing"


def test_a_warp_relaunch_isolates_storage_that_only_might_be_the_players(tmp_path: Path, monkeypatch) -> None:
    # The replaced game's savedir is another game's game/saves. Only storage
    # that certainly is the player's falls back to the server default; the
    # rest stays isolated.
    knobs = _relaunch_knobs(
        tmp_path,
        monkeypatch,
        _isolation(savedir_mode="path", home_mode="temporary", savedir=tmp_path / "scratch" / "game" / "saves"),
    )
    assert knobs == {"savedir": "temporary", "home": "temporary", "persistent": "empty", "preferences": "empty"}


def test_a_warp_relaunch_does_not_hand_a_linked_home_the_players_home(tmp_path: Path, monkeypatch) -> None:
    host_home = _host_home(tmp_path, monkeypatch)
    linked = _linked_home(tmp_path, host_home)
    knobs = _relaunch_knobs(
        tmp_path, monkeypatch, _isolation(savedir_mode="temporary", home_mode="path", home=linked)
    )
    assert knobs == {"savedir": "temporary", "home": "temporary", "persistent": "empty", "preferences": "empty"}


def test_a_relaunch_starting_from_an_isolated_game_is_recorded_as_isolated(tmp_path: Path) -> None:
    """A jump replacing an isolated game starts isolated, so an isolated request waits for it."""
    project = _make_project(tmp_path)
    key = live._key(project)
    live._SESSIONS[key] = _OwnedRunningSession(editor=True, isolation=_isolation("temporary"))
    release = threading.Event()

    def slow_launch(_root, _cancel):
        release.wait(5)
        return {"ok": False, "ready": False, "error": "stopped by the test"}

    try:
        jump = live.launch_requirements(project, warp="game/script.rpy:1", savedir="auto", home="auto",
                                        persistent="auto", preferences="auto")
        assert jump["isolated"] is True and jump["may_expose"] == frozenset()
        live.start_launch(str(project), slow_launch, wait_timeout=0, requirements=jump)
        waiting = live.start_launch(str(project), slow_launch, wait_timeout=0,
                                    requirements=live.launch_requirements(project, savedir="temporary"))
        assert waiting["code"] == "LAUNCH_IN_PROGRESS"
    finally:
        release.set()
        task = live._LAUNCHES.get(key)
        if task is not None:
            task.done_event.wait(5)
        live._LAUNCHES.pop(key, None)
        live._SESSIONS.pop(key, None)


def test_a_starting_launch_counts_storage_that_might_be_the_players(tmp_path: Path) -> None:
    """While it starts, a launch into another game's game/saves is not an isolated session."""
    project = _make_project(tmp_path)
    release = threading.Event()

    def slow_launch(_root, _cancel):
        release.wait(5)
        return {"ok": False, "ready": False, "error": "stopped by the test"}

    try:
        starting = live.launch_requirements(project, savedir=str(tmp_path / "scratch" / "game" / "saves"),
                                            home="temporary")
        live.start_launch(str(project), slow_launch, wait_timeout=0, requirements=starting)
        refused = live.start_launch(str(project), slow_launch, wait_timeout=0,
                                    requirements=live.launch_requirements(project, savedir="temporary"))
        assert refused["code"] == "SESSION_ISOLATION_MISMATCH"
    finally:
        release.set()
        task = live._LAUNCHES.get(live._key(project))
        if task is not None:
            task.done_event.wait(5)
        live._LAUNCHES.pop(live._key(project), None)


def test_a_launch_with_a_start_label_replaces_a_running_session(tmp_path: Path, monkeypatch) -> None:
    """The label reaches the game only at startup, so a reused game would never go there."""
    project = _make_project(tmp_path)
    key = live._key(project)
    running = _OwnedRunningSession(editor=False)
    live._SESSIONS[key] = running
    captured: dict = {}

    def fake_launch(_sdk, _project, **kwargs):
        captured.update(kwargs.get("extra_env") or {})
        return _LaunchedSession(editor=False)

    monkeypatch.setattr(live, "get_or_install_sdk", lambda *_a, **_k: "sdk")
    monkeypatch.setattr(live, "launch_with_bridge", fake_launch)
    try:
        result = live.launch_game(str(project), editor=False, savedir="temporary", start_label="chapter_two")
    finally:
        live._SESSIONS.pop(key, None)
    assert result["ok"] is True and result["already_running"] is False
    assert running.close_calls == 1
    assert captured["RENFORGE_BRIDGE_START_LABEL"] == "chapter_two"


def test_a_launch_with_a_start_label_refuses_to_take_over_an_external_bridge(tmp_path: Path, monkeypatch) -> None:
    project = _make_project(tmp_path)
    monkeypatch.setattr(
        live.BridgeClient,
        "from_project",
        classmethod(lambda cls, root, **_kwargs: _StateClient()),
    )
    monkeypatch.setattr(
        live,
        "get_or_install_sdk",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("must not relaunch")),
    )

    result = live.launch_game(str(project), editor=False, start_label="chapter_two")

    assert result["ok"] is False
    assert result["code"] == "BRIDGE_PROJECT_LOCKED"


@pytest.mark.parametrize("selector", ["banana", "8.x"])
def test_a_malformed_version_selector_is_invalid_not_unsupported(tmp_path, monkeypatch, selector):
    project = _make_project(tmp_path)
    monkeypatch.setattr(live, "get_or_install_sdk", lambda *args, **kwargs: pytest.fail("must not resolve/download SDK"))
    result = live.launch_game(str(project), version=selector)
    assert result["ok"] is False
    assert result["code"] == "INVALID_RENPY_VERSION"
    assert result["phase"] == "checking_runtime_compatibility"
