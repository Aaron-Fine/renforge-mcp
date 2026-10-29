"""Tests for stopping a game through the published bridge (cross-process)."""

from __future__ import annotations

import json
import tempfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

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


def _isolation(savedir_mode: str = "temporary", *, savedir: Path | None = None, home: Path | None = None):
    from renforge.save_isolation import LaunchIsolation

    if savedir_mode == "existing":
        return LaunchIsolation(savedir_mode="existing", home_mode="existing", persistent_mode="existing",
                               preferences_mode="existing", savedir=None, home=None, session_root=None,
                               cleanup=False)
    return LaunchIsolation(savedir_mode=savedir_mode, home_mode="temporary" if home is None else "path",
                           persistent_mode="existing", preferences_mode="existing",
                           savedir=savedir or _SESSION_ROOT / "saves", home=home or _SESSION_ROOT / "home",
                           session_root=_SESSION_ROOT, cleanup=False)


@pytest.fixture(autouse=True)
def _never_launch_a_real_game(monkeypatch):
    """A reuse that falls through would start the real SDK; tests that launch patch this themselves."""
    monkeypatch.setattr(live, "get_or_install_sdk",
                        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("must not launch a game")))


class _OwnedRunningSession:
    def __init__(self, *, editor: bool = False, isolation=None, bridge_eval: bool = True) -> None:
        self.editor = editor
        self.client = _StateClient()
        self.process = _RunningProcess()
        self.isolation = isolation
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
    assert live.stop_game(str(project)) == {"ok": True, "was_running": False}


def test_stop_game_issues_authenticated_quit_when_bridge_alive(
    tmp_path: Path,
    monkeypatch,
) -> None:
    project = _make_project(tmp_path, with_ready_bridge=True)
    client = _QuitClient()
    _patch_bridge_client(monkeypatch, client)

    result = live.stop_game(str(project))

    assert result == {"ok": True, "was_running": True}
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

    assert result == {"ok": True, "was_running": False}
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
    assert {key: matched[key] for key in ("ok", "already_running", "ready", "current_label", "editor")} == {
        "ok": True,
        "already_running": True,
        "ready": True,
        "current_label": "dashboard_scene",
        "editor": True,
    }
    assert matched["isolation"]["exposes_user_saves"] is False
    assert live._SESSIONS[key] is session
    assert session.close_calls == 0
    live._SESSIONS.pop(key, None)


def test_an_isolated_launch_never_reuses_a_session_on_the_players_saves(tmp_path: Path) -> None:
    """A default launch is mutating; handing back a savedir=existing session would skip authorize."""
    project = _make_project(tmp_path)
    key = live._key(project)
    session = _OwnedRunningSession(editor=True, isolation=_isolation("existing"))
    live._SESSIONS[key] = session
    try:
        refused = live.launch_game(str(project))
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
    """savedir=<another app's folder> needed authorize; a default launch must not inherit it."""
    project = _make_project(tmp_path)
    key = live._key(project)
    foreign = tmp_path / "Documents" / "game-saves"
    live._SESSIONS[key] = _OwnedRunningSession(editor=True, isolation=_isolation("path", savedir=foreign))
    try:
        assert live.launch_game(str(project))["code"] == "SESSION_ISOLATION_MISMATCH"
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

    exposed = {"isolated": False, "eval": True}
    try:
        live.start_launch(str(project), slow_launch, wait_timeout=0, requirements=exposed)
        refused = live.start_launch(str(project), slow_launch, wait_timeout=0,
                                    requirements={"isolated": True, "eval": True})
        assert refused["code"] == "SESSION_ISOLATION_MISMATCH"
        refused = live.start_launch(str(project), slow_launch, wait_timeout=0,
                                    requirements={"isolated": False, "eval": False})
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


class _InfoClient(_StateClient):
    def __init__(self, info: dict | None) -> None:
        self.info = info

    def session_info(self) -> dict:
        if self.info is None:
            raise live.BridgeError("bridge error on 'session_info': unknown command")
        return self.info


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
    monkeypatch.setattr(live.BridgeClient, "from_project",
                        classmethod(lambda cls, root, **_kwargs: _InfoClient(info)))
    result = live.launch_game(str(project), editor=False)

    if code is None:
        assert result["already_running"] is True and result["external"] is True
    else:
        assert result["ok"] is False and result["code"] == code


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
    assert launch_kwargs["savedir"] == "temporary"
    assert launch_kwargs["home"] == "temporary"
    assert launch_kwargs["persistent"] == "empty"
    assert launch_kwargs["preferences"] == "empty"

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

    assert result == {
        "ok": True,
        "ready": True,
        "status": "ready",
        "current_label": "dashboard_scene",
        "editor": True,
        # No isolation recorded: status says the caller cannot count on one.
        "isolated": False,
        "eval_enabled": True,
    }
    live._SESSIONS.pop(key, None)


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
        # It used to surface as UnboundLocalError from the cleanup code.
        with pytest.raises(OSError, match="bridge socket gone"):
            live.stop_game(str(project))
    finally:
        live._LAUNCHES.pop(key, None)
        live._SESSIONS.pop(key, None)
