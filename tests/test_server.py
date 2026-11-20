import asyncio
import base64
import io
import json
import threading

import pytest

from renforge.server import _FallbackServer, _register_tools, create_app
from renforge.state import project_state_dir


class _ToolRegistry:
    _renforge_testing_registry = True

    def __init__(self) -> None:
        self.tools = {}

    def tool(self):
        def register(fn):
            self.tools[fn.__name__] = fn
            return fn

        return register


class _MetadataToolRegistry:
    def __init__(self, *, fail_registration: bool = False) -> None:
        self.tools = {}
        self.metadata = {}
        self.registration_calls = 0
        self.fail_registration = fail_registration

    def tool(self, *, description=None, annotations=None):
        self.registration_calls += 1

        def register(fn):
            if self.fail_registration:
                raise TypeError("internal registration failure")
            self.tools[fn.__name__] = fn
            self.metadata[fn.__name__] = {
                "description": description,
                "annotations": annotations,
            }
            return fn

        return register


class _MetadataDroppingBackend:
    def tool(self):
        return lambda fn: fn

EXPECTED_TOOLS = {
    # static
    "renforge_info",
    "renforge_context",
    "renforge_inspect_project",
    "renforge_scan_project",
    "renforge_parse_lint",
    "renforge_validate",
    "renforge_inspect_image",
    "renforge_find_references",
    # live game control
    "renforge_launch",
    "renforge_launch_status",
    "renforge_jump",
    "renforge_new_game",
    "renforge_stop",
    "renforge_game_state",
    "renforge_inspect_screen",
    "renforge_game_state_compact",
    "renforge_advance",
    "renforge_observe",
    "renforge_act",
    "renforge_session",
    "renforge_control",
    "renforge_send_input",
    "renforge_saves",
    "renforge_mod",
    "renforge_list_choices",
    "renforge_select_choice",
    "renforge_list_ui_elements",
    "renforge_click_element",
    "renforge_hover_element",
    "renforge_get_ui_element_bounds",
    "renforge_editor",
    "renforge_click_at",
    "renforge_capture_screenshot",
    "renforge_estimate_translation",
    "renforge_eval",
    "renforge_get_var",
    "renforge_set_var",
    "renforge_poll_events",
    "renforge_get_errors",
    "renforge_wait_until",
    "renforge_screenshot",
    "renforge_find_image_on_screen",
    "renforge_get_displayable_bounds",
    "renforge_position_element",
    "renforge_diff_screenshots",
    "renforge_scene_tree",
    "renforge_explain",
    "renforge_measure",
    "renforge_hit_test",
    "renforge_run_scenario",
    "renforge_autopilot",
    # assets / translation / build / docs
    "renforge_assets",
    "renforge_languages",
    "renforge_translation_stats",
    "renforge_generate_translations",
    "renforge_export_dialogue",
    "renforge_web_build",
    "renforge_distribute",
    "renforge_search_docs",
    "renforge_get_doc",
    "renforge_list_docs",
}


def test_fallback_server_runs_cleanly() -> None:
    assert _FallbackServer().run() == 0


def test_register_tools_forwards_metadata_to_compatible_backends() -> None:
    from renforge.tool_definitions import TOOL_DEFINITIONS

    app = _MetadataToolRegistry()

    _register_tools(app)

    assert set(app.tools) == set(TOOL_DEFINITIONS)
    metadata = app.metadata["renforge_eval"]
    assert metadata["description"] == TOOL_DEFINITIONS["renforge_eval"].description
    assert metadata["annotations"].destructiveHint is True


def test_register_tools_preserves_metadata_on_mcp_fastmcp_backend() -> None:
    fastmcp_module = pytest.importorskip("mcp.server.fastmcp", reason="MCP FastMCP unavailable")
    from renforge.tool_definitions import TOOL_DEFINITIONS

    app = fastmcp_module.FastMCP("renforge-test")
    _register_tools(app)

    tool = app._tool_manager._tools["renforge_saves"]
    assert tool.description == TOOL_DEFINITIONS["renforge_saves"].description
    assert tool.annotations.destructiveHint is True
    assert tool.parameters["properties"]["action"]["enum"] == [
        "save",
        "load",
        "list",
        "list_user",
        "import",
    ]
    assert len(tool.parameters["oneOf"]) == 5


def test_register_tools_does_not_retry_internal_decorator_type_errors() -> None:
    app = _MetadataToolRegistry(fail_registration=True)

    with pytest.raises(TypeError, match="internal registration failure"):
        _register_tools(app)

    assert app.registration_calls == 1


def test_register_tools_fails_closed_when_backend_cannot_accept_metadata() -> None:
    with pytest.raises(RuntimeError, match="description.*annotations"):
        _register_tools(_MetadataDroppingBackend())


def test_get_var_logs_activity_because_property_resolution_can_run_user_code(
    tmp_path, monkeypatch
) -> None:
    from renforge.tools import live

    monkeypatch.setattr(live, "get_var", lambda project_path, name: {"ok": True, "value": 7})
    app = _MetadataToolRegistry()
    _register_tools(app)

    result = app.tools["renforge_get_var"](str(tmp_path), "score")

    assert result == {"ok": True, "value": 7}
    entry = json.loads(
        (project_state_dir(tmp_path) / "activity.jsonl").read_text(encoding="utf-8")
    )
    assert entry["name"] == "renforge_get_var"
    assert entry["params"]["name"] == "score"


def test_mutable_tool_call_logs_bounded_redacted_activity(tmp_path, monkeypatch) -> None:
    from renforge.tools import live

    secret = "private-value-" + ("x" * 20_000)
    monkeypatch.setattr(
        live,
        "set_var",
        lambda project_path, name, value: {"ok": True, "value": value, "debug": secret},
    )
    app = _MetadataToolRegistry()
    _register_tools(app)

    result = app.tools["renforge_set_var"](str(tmp_path), "api_token", secret)

    assert result["ok"] is True
    raw = (project_state_dir(tmp_path) / "activity.jsonl").read_text(encoding="utf-8")
    assert len(raw.encode("utf-8")) <= 8192
    assert secret not in raw
    entry = json.loads(raw)
    assert entry["name"] == "renforge_set_var"
    assert entry["params"]["value"] == "[redacted]"
    assert entry["result"] == {"ok": True, "keys": ["debug", "ok", "value"]}


def test_game_state_compact_prefix_selection_enforces_all_output_limits(
    tmp_path, monkeypatch
) -> None:
    from renforge.tools import live

    variables = {
        f"large_{index}": {"nested": {"payload": "x" * 2_000}}
        for index in range(20)
    }
    monkeypatch.setattr(
        live,
        "game_state",
        lambda project_path: {"ok": True, "current_label": "start", "variables": variables},
    )
    app = _MetadataToolRegistry()
    _register_tools(app)

    result = app.tools["renforge_game_state_compact"](
        str(tmp_path),
        variable_prefix="large_",
        state_profile="full",
        max_depth=2,
        max_items=2,
        max_output_bytes=256,
    )

    assert len(json.dumps(result).encode("utf-8")) <= 256
    assert len([key for key in result.get("variables", {}) if not key.startswith("__")]) <= 2
    assert "x" * 2_000 not in json.dumps(result)
    assert "__truncated__" in json.dumps(result)


def test_create_app_registers_expected_tools() -> None:
    from renforge.tool_definitions import TOOL_DEFINITIONS

    app = create_app()
    if isinstance(app, _FallbackServer):
        pytest.skip("MCP backend (mcp/fastmcp) not installed")

    tools = asyncio.run(app.list_tools())
    names = {tool.name for tool in tools if tool.name.startswith("renforge_")}
    assert len(EXPECTED_TOOLS) == 61
    assert EXPECTED_TOOLS == set(TOOL_DEFINITIONS) == names
    instructions = getattr(app, "instructions", "") or ""
    assert "renforge_info" in instructions or not hasattr(app, "instructions")
    # The instructions teach the play loop: one tool per task.
    for tool in ("renforge_session", "renforge_observe", "renforge_act", "renforge_explain", "renforge_mod"):
        assert tool in instructions or not hasattr(app, "instructions")


def test_session_load_returns_the_restored_observation(tmp_path, monkeypatch) -> None:
    pytest.importorskip("fastmcp", reason="fastmcp not installed")
    from fastmcp import Client

    from renforge.tools import live

    # The bridge answers before the engine restarts into the save, naming the
    # old label; the session waits for the restored game instead.
    monkeypatch.setattr(live, "saves", lambda *a, **k: {
        "ok": True, "slot": "a", "restored_label": "before_load", "restarting": True})
    snapshots = iter([{"stable": False}, {"stable": True, "observation_id": "obs_2", "label": "loaded",
                                          "controls": [], "dialogue": None}])
    monkeypatch.setattr(live, "observe", lambda *a, **k: next(snapshots))
    monkeypatch.setattr("time.sleep", lambda _s: None)

    async def _call():
        async with Client(create_app()) as client:
            return await client.call_tool("renforge_session",
                                          {"project_path": str(tmp_path), "action": "load", "slot": "a"})

    payload = json.loads(next(block.text for block in asyncio.run(_call()).content if block.type == "text"))
    assert payload == {"ok": True, "slot": "a",
                       "observation": {"stable": True, "observation_id": "obs_2", "label": "loaded", "controls": []}}


def test_session_lists_and_imports_the_players_slots(tmp_path, monkeypatch) -> None:
    pytest.importorskip("fastmcp", reason="fastmcp not installed")
    from fastmcp import Client

    from renforge.state import project_state_dir
    from renforge.tools import live

    from renforge.save_isolation import host_renpy_root

    home = tmp_path / "home"
    # Ren'Py's save root is ~/.renpy, ~/Library/RenPy, or %APPDATA%/RenPy.
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("APPDATA", str(home / "AppData" / "Roaming"))
    player = host_renpy_root(home=home) / "archived-game"
    player.mkdir(parents=True)
    (player / "1-1-LT1.save").write_bytes(b"PLAYER")
    monkeypatch.setenv("RENFORGE_STATE_DIR", str(tmp_path / "state"))
    project = tmp_path / "project"
    (project / "game").mkdir(parents=True)
    # Only the running game knows its save folder when it ships no loose .rpy.
    monkeypatch.setattr(live, "saves", lambda *a, **k: {"ok": True, "slots": [], "save_directory": "archived-game"})

    async def _call(**args):
        async with Client(create_app()) as client:
            result = await client.call_tool("renforge_session", {"project_path": str(project), **args})
        return json.loads(next(block.text for block in result.content if block.type == "text"))

    assert asyncio.run(_call(action="list")) == {"ok": True, "slots": [], "player_slots": [{"name": "1-1"}]}
    assert asyncio.run(_call(action="import", slot="1-1"))["replaced"] is False
    assert (project_state_dir(project) / "saves" / "1-1-LT1.save").read_bytes() == b"PLAYER"
    assert asyncio.run(_call(action="import", slot="1-1"))["replaced"] is True
    assert asyncio.run(_call(action="import", slot="9-9"))["ok"] is False
    assert (player / "1-1-LT1.save").read_bytes() == b"PLAYER"

    # Under enforce, reading the player's saves needs authorize, as for list_user.
    monkeypatch.setenv("RENFORGE_POLICY", "enforce")
    hidden = asyncio.run(_call(action="list"))
    assert "player_slots" not in hidden and "authorize=true" in hidden["player_slots_hidden"]
    assert asyncio.run(_call(action="list", authorize=True))["player_slots"] == [{"name": "1-1"}]
    assert asyncio.run(_call(action="import", slot="1-1"))["code"] == "POLICY_DENIED"
    assert asyncio.run(_call(action="import", slot="1-1", authorize=True))["ok"] is True


class _SessionSavesClient:
    """A running game's bridge: where it keeps its saves, and one slot in them."""

    def __init__(self, info: dict) -> None:
        self.info = info
        self.calls: list[str] = []

    def session_info(self) -> dict:
        return dict(self.info)

    def list_slots(self, regexp=None) -> dict:
        self.calls.append("list")
        return {"ok": True, "slots": [{"name": "player-slot"}], "save_directory": "a-game"}

    def save_slot(self, slot, extra_info="") -> dict:
        self.calls.append("save")
        return {"ok": True, "slot": slot}

    def load_slot(self, slot) -> dict:
        self.calls.append("load")
        return {"ok": True, "slot": slot}


def test_session_import_goes_where_the_running_game_keeps_its_saves(tmp_path, monkeypatch) -> None:
    """A load reads the running game's own save folder, so an import must land there."""
    import contextlib
    import tempfile
    from types import SimpleNamespace

    from renforge.save_isolation import host_renpy_root
    from renforge.tools import live

    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("APPDATA", str(home / "AppData" / "Roaming"))
    monkeypatch.setenv("RENFORGE_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    player = host_renpy_root(home=home) / "a-game"
    player.mkdir(parents=True)
    (player / "1-1-LT1.save").write_bytes(b"PLAYER")
    project = tmp_path / "project"
    (project / "game").mkdir(parents=True)
    session_root = tmp_path / "renforge-session-x"
    session_saves = session_root / "saves"
    session_saves.mkdir(parents=True)
    client = _SessionSavesClient({"eval_enabled": True, "savedir": str(session_saves),
                                  "home": str(session_root / "home"), "save_locations": [str(session_saves)]})
    monkeypatch.setattr(live.BridgeClient, "from_project", classmethod(lambda cls, root, **_k: client))
    app = _ToolRegistry()
    _register_tools(app)
    session_tool = app.tools["renforge_session"]

    # A game this server did not start: its folder is not one it may write into.
    refused = session_tool(str(project), "import", slot="1-1")
    assert refused["ok"] is False and refused["code"] == "SESSION_SAVES_ELSEWHERE"
    assert not (project_state_dir(project) / "saves").exists()

    class _OwnedTemporary:
        temporary_savedir = session_saves
        isolation = SimpleNamespace(savedir_mode="temporary", session_root=session_root)

        @contextlib.contextmanager
        def holding_open(self):
            yield True

    monkeypatch.setitem(live._SESSIONS, live._key(project), _OwnedTemporary())
    imported = session_tool(str(project), "import", slot="1-1")
    assert imported["ok"] is True and imported["slot"] == "1-1" and imported["replaced"] is False
    assert (session_saves / "1-1-LT1.save").read_bytes() == b"PLAYER"
    assert not (project_state_dir(project) / "saves").exists()
    assert session_tool(str(project), "import", slot="1-1")["replaced"] is True


def test_session_import_never_guesses_the_folder_when_the_bridge_does_not_answer(tmp_path, monkeypatch) -> None:
    """A busy bridge is not 'no game': the import must not land where the running game never loads it."""
    import contextlib
    import tempfile
    from types import SimpleNamespace

    from renforge.bridge.client import BridgeNotRunningError
    from renforge.save_isolation import host_renpy_root
    from renforge.tools import live

    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("APPDATA", str(home / "AppData" / "Roaming"))
    monkeypatch.setenv("RENFORGE_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    player = host_renpy_root(home=home) / "a-game"
    player.mkdir(parents=True)
    (player / "1-1-LT1.save").write_bytes(b"PLAYER")
    project = tmp_path / "project"
    (project / "game").mkdir(parents=True)
    (project / "game" / "options.rpy").write_text('define config.save_directory = "a-game"\n', encoding="utf-8")
    busy = _UnansweringSessionClient("bridge request timed out")
    monkeypatch.setattr(live.BridgeClient, "from_project", classmethod(lambda cls, root, **_k: busy))
    app = _ToolRegistry()
    _register_tools(app)
    session_tool = app.tools["renforge_session"]
    state_saves = project_state_dir(project) / "saves"

    # A game this server did not start, and that does not answer: retry, write nothing.
    refused = session_tool(str(project), "import", slot="1-1")
    assert refused["ok"] is False and refused["files_touched"] == [], refused
    assert "timed out" in refused["error"]
    assert not state_saves.exists()

    # A game still starting has not said where it saves either.
    def starting(cls, root, **_k):
        raise BridgeNotRunningError("the game is still starting", code="GAME_STARTING")

    monkeypatch.setattr(live.BridgeClient, "from_project", classmethod(starting))
    assert session_tool(str(project), "import", slot="1-1")["ok"] is False
    assert not state_saves.exists()

    # A game this server launched with savedir='temporary': its folder is known here.
    monkeypatch.setattr(live.BridgeClient, "from_project", classmethod(lambda cls, root, **_k: busy))
    session_root = tmp_path / "renforge-session-x"
    session_saves = session_root / "saves"
    session_saves.mkdir(parents=True)

    class _OwnedTemporary:
        temporary_savedir = session_saves
        isolation = SimpleNamespace(savedir_mode="temporary", session_root=session_root)

        @contextlib.contextmanager
        def holding_open(self):
            yield True

    monkeypatch.setitem(live._SESSIONS, live._key(project), _OwnedTemporary())
    imported = session_tool(str(project), "import", slot="1-1")
    assert imported["ok"] is True, imported
    assert (session_saves / "1-1-LT1.save").read_bytes() == b"PLAYER"
    assert not state_saves.exists()

    # No game at all: the import goes to the session's saves, which the next launch uses.
    monkeypatch.delitem(live._SESSIONS, live._key(project))

    def not_running(cls, root, **_k):
        raise BridgeNotRunningError("no game", code="NO_RUNNING_GAME")

    monkeypatch.setattr(live.BridgeClient, "from_project", classmethod(not_running))
    assert session_tool(str(project), "import", slot="1-1")["ok"] is True
    assert (state_saves / "1-1-LT1.save").read_bytes() == b"PLAYER"


@pytest.mark.parametrize("policy", ["off", "enforce"])
def test_session_saves_never_act_on_a_game_using_the_players_saves(tmp_path, monkeypatch, policy) -> None:
    """renforge_session promises the state folder, never the player's saves."""
    pytest.importorskip("fastmcp", reason="fastmcp not installed")
    from fastmcp import Client

    from renforge.save_isolation import host_renpy_root
    from renforge.tools import live

    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("APPDATA", str(home / "AppData" / "Roaming"))
    monkeypatch.setenv("RENFORGE_POLICY", policy)
    project = tmp_path / "project"
    (project / "game").mkdir(parents=True)
    player_saves = host_renpy_root(home=home) / "a-game"
    player_saves.mkdir(parents=True)
    client = _SessionSavesClient({"eval_enabled": True, "savedir": str(player_saves), "home": str(home),
                                  "save_locations": [str(player_saves)]})
    monkeypatch.setattr(live.BridgeClient, "from_project", classmethod(lambda cls, root, **_k: client))

    async def _call(**args):
        async with Client(create_app()) as mcp:
            result = await mcp.call_tool("renforge_session", {"project_path": str(project), **args})
        return next(block.text for block in result.content if block.type == "text")

    for args in ({"action": "save", "slot": "x"}, {"action": "load", "slot": "x", "authorize": True},
                 {"action": "list"}):
        text = asyncio.run(_call(**args))
        refused = json.loads(text)
        assert refused["code"] == "SESSION_NOT_ISOLATED", args
        assert "player-slot" not in text
        # A refusal lists nothing, so it says nothing about anyone's slots.
        assert "player_slots" not in refused and "player_slots_hidden" not in refused
    assert client.calls == []

    # The same game with its saves in the state folder and a session HOME is the session's own.
    import tempfile

    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    state_saves = project_state_dir(project) / "saves"
    session_home = tmp_path / "renforge-session-x" / "home"
    for path in (state_saves, session_home):
        path.mkdir(parents=True)
    client.info = {"eval_enabled": True, "savedir": str(state_saves), "home": str(session_home),
                   "save_locations": [str(state_saves)]}
    listed = json.loads(asyncio.run(_call(action="list")))
    assert listed["ok"] is True and listed["slots"] == [{"name": "player-slot"}]
    if policy == "enforce":
        # The slots shown are the session's; the hidden ones are the player's.
        assert "session's own" in listed["player_slots_hidden"] and "player_slots" not in listed
    assert json.loads(asyncio.run(_call(action="save", slot="x")))["ok"] is True
    assert client.calls == ["list", "save"]


class _UnansweringSessionClient(_SessionSavesClient):
    """A bridge whose session_info fails: busy main thread, or a bridge without the command."""

    def __init__(self, error: str) -> None:
        super().__init__({})
        self.error = error

    def session_info(self) -> dict:
        from renforge.bridge.client import BridgeError

        raise BridgeError(f"bridge error on 'session_info': {self.error}")


@pytest.mark.parametrize("action", ["save", "load", "list"])
def test_session_saves_pass_a_busy_bridge_through_as_a_retryable_error(tmp_path, monkeypatch, action) -> None:
    """A busy game is not proof of the player's saves; telling the agent to relaunch would lose play state."""
    from renforge.tools import live

    (tmp_path / "game").mkdir()
    busy = _UnansweringSessionClient("timeout_waiting_for_main_thread")
    monkeypatch.setattr(live.BridgeClient, "from_project", classmethod(lambda cls, root, **_k: busy))
    slot = None if action == "list" else "x"

    result = live.saves(str(tmp_path), action, slot=slot, renforge_owned_only=True)

    assert result["ok"] is False
    assert result.get("code") != "SESSION_NOT_ISOLATED"
    assert "timeout_waiting_for_main_thread" in result["error"]
    assert busy.calls == []

    # A bridge that has no session_info cannot say where it saves: refused.
    old = _UnansweringSessionClient("unknown_command: session_info")
    monkeypatch.setattr(live.BridgeClient, "from_project", classmethod(lambda cls, root, **_k: old))
    assert live.saves(str(tmp_path), action, slot=slot, renforge_owned_only=True)["code"] == "SESSION_NOT_ISOLATED"
    assert old.calls == []


@pytest.mark.parametrize("slot", [
    "/home/player/.renpy/a-game/1-1", "../../player/1-1", "sub/1-1", "sub\\1-1", "C:1-1",
    "\\\\server\\share\\1-1", ".", "..", "1-1\x00",
])
@pytest.mark.parametrize("action", ["save", "load"])
def test_save_and_load_refuse_a_slot_that_is_a_path(tmp_path, monkeypatch, slot, action) -> None:
    """Ren'Py joins the slot onto its save folder, so a path slot would reach any folder, the player's included."""
    import tempfile

    from renforge.tools import live

    monkeypatch.setenv("RENFORGE_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    project = tmp_path / "project"
    (project / "game").mkdir(parents=True)
    # The session's own game: an ordinary slot name would be saved or loaded.
    state_saves = project_state_dir(project) / "saves"
    session_home = tmp_path / "renforge-session-x" / "home"
    for path in (state_saves, session_home):
        path.mkdir(parents=True)
    client = _SessionSavesClient({"eval_enabled": True, "savedir": str(state_saves), "home": str(session_home),
                                  "save_locations": [str(state_saves)]})
    monkeypatch.setattr(live.BridgeClient, "from_project", classmethod(lambda cls, root, **_k: client))
    monkeypatch.setattr(live, "wait_for_stable_observation", lambda *_a, **_k: {})
    app = _ToolRegistry()
    _register_tools(app)

    for result in (live.saves(str(project), action, slot=slot),
                   live.saves(str(project), action, slot=slot, renforge_owned_only=True),
                   app.tools["renforge_session"](str(project), action, slot=slot, authorize=True)):
        assert result["ok"] is False and result["code"] == "SAVE_SLOT_INVALID", result
    assert client.calls == []
    assert live.saves(str(project), action, slot="quick-1", renforge_owned_only=True)["ok"] is True


def test_live_tools_error_cleanly_without_a_running_game(tmp_path) -> None:
    from renforge.tools import live

    # A valid project directory but no running bridge.
    (tmp_path / "game").mkdir()
    result = live.game_state(str(tmp_path))
    assert result["ok"] is False
    assert "error" in result

    # An invalid project path is reported, not raised.
    launched = live.launch_game(str(tmp_path / "does-not-exist"))
    assert launched["ok"] is False


def test_info_reports_the_project_selected_in_the_dashboard(tmp_path, monkeypatch) -> None:
    fastmcp = pytest.importorskip("fastmcp", reason="fastmcp not installed")
    from fastmcp import Client

    from renforge.session_registry import publish_dashboard

    monkeypatch.setenv("RENFORGE_RUNTIME_DIR", str(tmp_path / "runtime"))
    project = tmp_path / "game-project"
    publish_dashboard(project, url="http://127.0.0.1:8765/", token="secret_token_22chars__")

    async def _call():
        async with Client(create_app()) as client:
            return await client.call_tool("renforge_info", {})

    result = asyncio.run(_call())
    payload = json.loads(next(block.text for block in result.content if block.type == "text"))
    assert payload["active_project"] == str(project.resolve())
    assert payload["dashboard"]["url"] == "http://127.0.0.1:8765/"


def test_info_falls_back_to_the_server_default_project(tmp_path, monkeypatch) -> None:
    pytest.importorskip("fastmcp", reason="fastmcp not installed")
    from fastmcp import Client

    monkeypatch.setenv("RENFORGE_RUNTIME_DIR", str(tmp_path / "empty-runtime"))
    app = create_app()
    app.project_root = tmp_path

    async def _call():
        async with Client(app) as client:
            return await client.call_tool("renforge_info", {})

    result = asyncio.run(_call())
    payload = json.loads(next(block.text for block in result.content if block.type == "text"))
    assert payload["active_project"] == str(tmp_path.resolve())
    assert payload["project_source"] == "serve_default"
    assert payload["dashboard"] is None


def test_info_detects_a_project_from_the_current_directory(tmp_path, monkeypatch) -> None:
    pytest.importorskip("fastmcp", reason="fastmcp not installed")
    from fastmcp import Client

    monkeypatch.setenv("RENFORGE_RUNTIME_DIR", str(tmp_path / "empty-runtime"))
    project = tmp_path / "my-game"
    game = project / "game"
    game.mkdir(parents=True)
    (game / "script.rpy").write_text("label start:\n    return\n")
    monkeypatch.chdir(project)

    async def _call():
        async with Client(create_app()) as client:
            return await client.call_tool("renforge_info", {})

    result = asyncio.run(_call())
    payload = json.loads(next(block.text for block in result.content if block.type == "text"))
    assert payload["active_project"] == str(project.resolve())
    assert payload["project_source"] == "cwd"


def test_info_explains_the_fallback_when_nothing_matches(tmp_path, monkeypatch) -> None:
    pytest.importorskip("fastmcp", reason="fastmcp not installed")
    from fastmcp import Client

    monkeypatch.setenv("RENFORGE_RUNTIME_DIR", str(tmp_path / "empty-runtime"))
    monkeypatch.chdir(tmp_path)

    async def _call():
        async with Client(create_app()) as client:
            return await client.call_tool("renforge_info", {})

    result = asyncio.run(_call())
    payload = json.loads(next(block.text for block in result.content if block.type == "text"))
    assert payload["active_project"] is None
    assert payload["project_source"] is None
    assert "project_path" in payload["hint"]


def test_scan_tool_accepts_bounded_queries(tmp_path) -> None:
    pytest.importorskip("fastmcp", reason="fastmcp not installed")
    from fastmcp import Client

    game = tmp_path / "game"
    game.mkdir()
    (game / "script.rpy").write_text(
        "label start:\n    jump ending\nlabel ending:\n    return\n",
        encoding="utf-8",
    )

    async def _call():
        async with Client(create_app()) as client:
            return await client.call_tool(
                "renforge_scan_project",
                {
                    "project_path": str(tmp_path),
                    "sections": ["labels"],
                    "symbol": "ending",
                    "limit": 1,
                },
            )

    result = asyncio.run(_call())
    payload = json.loads(next(block.text for block in result.content if block.type == "text"))
    assert payload["labels"] == [{"file": "game/script.rpy", "line": 3, "name": "ending"}]
    assert "jumps" not in payload


def test_scan_tool_defaults_to_summary_only(tmp_path) -> None:
    pytest.importorskip("fastmcp", reason="fastmcp not installed")
    from fastmcp import Client

    game = tmp_path / "game"
    game.mkdir()
    (game / "script.rpy").write_text("label start:\n    return\n", encoding="utf-8")

    async def _call():
        async with Client(create_app()) as client:
            return await client.call_tool(
                "renforge_scan_project",
                {"project_path": str(tmp_path)},
            )

    result = asyncio.run(_call())
    payload = json.loads(next(block.text for block in result.content if block.type == "text"))
    assert set(payload) == {"summary", "pagination"}


def test_launch_tool_forwards_a_warp_target(tmp_path, monkeypatch) -> None:
    (tmp_path / "game").mkdir()
    pytest.importorskip("fastmcp", reason="fastmcp not installed")
    from fastmcp import Client

    from renforge.tools import live

    calls = {}

    def fake_launch(project_path: str, version: str = "stable", warp: str | None = None, **kwargs):
        calls.update(project_path=project_path, version=version, warp=warp)
        calls["kwargs"] = kwargs
        return {"ok": True, "current_label": "chapter_two"}

    monkeypatch.setattr(live, "launch_game", fake_launch)

    async def _call():
        async with Client(create_app()) as client:
            return await client.call_tool(
                "renforge_launch",
                {"project_path": str(tmp_path), "warp": "game/script.rpy:42"},
            )

    result = asyncio.run(_call())
    assert result.is_error is False
    assert calls["project_path"] == str(tmp_path)
    assert calls["version"] == "stable"
    assert calls["warp"] == "game/script.rpy:42"
    assert calls["kwargs"]["editor"] is True


def test_launch_tool_defaults_editor_true(tmp_path, monkeypatch) -> None:
    (tmp_path / "game").mkdir()
    from renforge import dashboard_client
    from renforge.tools import live

    calls = {}
    monkeypatch.setattr(dashboard_client, "launch_game", lambda *_args, **_kwargs: None)

    def fake_launch(project_path: str, version: str = "stable", warp: str | None = None, **kwargs):
        calls.update(project_path=project_path, version=version, warp=warp)
        calls["kwargs"] = kwargs
        return {"ok": True, "ready": True, "already_running": False, "editor": kwargs.get("editor", False)}

    monkeypatch.setattr(live, "launch_game", fake_launch)

    app = _ToolRegistry()
    _register_tools(app)
    result = app.tools["renforge_launch"](str(tmp_path))

    assert result["ok"] is True
    assert calls["project_path"] == str(tmp_path)
    assert calls["version"] == "stable"
    assert calls["warp"] is None
    assert calls["kwargs"]["editor"] is True
    assert calls["kwargs"]["savedir"] == "existing"
    assert calls["kwargs"]["home"] == "existing"
    assert calls["kwargs"]["persistent"] == "existing"
    assert calls["kwargs"]["preferences"] == "existing"


def test_delegated_isolated_launch_forwards_language(tmp_path, monkeypatch) -> None:
    (tmp_path / "game").mkdir()
    from renforge import dashboard_client

    monkeypatch.delenv("RENFORGE_ISOLATION", raising=False)
    calls = {}

    def fake_dashboard_launch(_project_path, **kwargs):
        calls.update(kwargs)
        return {
            "ok": True,
            "ready": True,
            "already_running": False,
            "requested_language": kwargs.get("language"),
            "active_language": kwargs.get("language"),
            "language_applied": True,
        }

    monkeypatch.setattr(dashboard_client, "launch_game", fake_dashboard_launch)

    app = _ToolRegistry()
    _register_tools(app)
    result = app.tools["renforge_launch"](str(tmp_path), savedir="temporary", language="japanese")

    assert result["ok"] is True
    assert calls["language"] == "japanese"
    assert calls["savedir"] == "temporary" and calls["home"] == "temporary"
    assert "warning" not in result


def test_launch_tool_forwards_editor_mode_to_direct_launch(tmp_path, monkeypatch) -> None:
    (tmp_path / "game").mkdir()
    from renforge import dashboard_client
    from renforge.tools import live

    calls = {}
    monkeypatch.setattr(dashboard_client, "launch_game", lambda *_args, **_kwargs: None)

    def fake_launch(project_path: str, version: str = "stable", warp: str | None = None, **kwargs):
        calls.update(project_path=project_path, version=version, warp=warp)
        calls["kwargs"] = kwargs
        return {"ok": True, "ready": True, "already_running": False, "editor": kwargs.get("editor", False)}

    monkeypatch.setattr(live, "launch_game", fake_launch)

    app = _ToolRegistry()
    _register_tools(app)
    result = app.tools["renforge_launch"](str(tmp_path), editor=True)

    assert result["ok"] is True
    assert calls["project_path"] == str(tmp_path)
    assert calls["version"] == "stable"
    assert calls["warp"] is None
    assert calls["kwargs"]["editor"] is True
    assert calls["kwargs"]["savedir"] == "existing"
    assert calls["kwargs"]["home"] == "existing"
    assert calls["kwargs"]["persistent"] == "existing"
    assert calls["kwargs"]["preferences"] == "existing"


def test_launch_tool_honors_editor_opt_out(tmp_path, monkeypatch) -> None:
    (tmp_path / "game").mkdir()
    from renforge import dashboard_client
    from renforge.tools import live

    calls = {}
    monkeypatch.setattr(dashboard_client, "launch_game", lambda *_args, **_kwargs: None)

    def fake_launch(project_path: str, version: str = "stable", warp: str | None = None, **kwargs):
        calls.update(project_path=project_path, version=version, warp=warp)
        calls["kwargs"] = kwargs
        return {
            "ok": True,
            "ready": True,
            "already_running": False,
            "editor": kwargs.get("editor"),
        }

    monkeypatch.setattr(live, "launch_game", fake_launch)

    app = _ToolRegistry()
    _register_tools(app)
    result = app.tools["renforge_launch"](str(tmp_path), editor=False)

    assert result["ok"] is True
    assert calls["kwargs"]["editor"] is False


def test_launch_tool_can_opt_into_existing_saves(tmp_path, monkeypatch) -> None:
    (tmp_path / "game").mkdir()
    from renforge import dashboard_client
    from renforge.tools import live

    calls = {}
    monkeypatch.setattr(dashboard_client, "launch_game", lambda *_args, **_kwargs: None)

    def fake_launch(project_path: str, version: str = "stable", warp: str | None = None, **kwargs):
        calls["kwargs"] = kwargs
        return {"ok": True, "ready": True, "already_running": False, "editor": True}

    monkeypatch.setattr(live, "launch_game", fake_launch)

    app = _ToolRegistry()
    _register_tools(app)
    result = app.tools["renforge_launch"](str(tmp_path), savedir="existing")

    assert result["ok"] is True
    assert calls["kwargs"]["savedir"] == "existing"
    assert calls["kwargs"]["home"] == "existing"


def test_launch_tool_honors_renforge_isolation_env(tmp_path, monkeypatch) -> None:
    (tmp_path / "game").mkdir()
    from renforge import dashboard_client
    from renforge.tools import live

    calls = {}
    monkeypatch.setenv("RENFORGE_ISOLATION", "existing")
    monkeypatch.setattr(dashboard_client, "launch_game", lambda *_args, **_kwargs: None)

    def fake_launch(project_path: str, version: str = "stable", warp: str | None = None, **kwargs):
        calls["kwargs"] = kwargs
        return {"ok": True, "ready": True, "already_running": False, "editor": True}

    monkeypatch.setattr(live, "launch_game", fake_launch)

    app = _ToolRegistry()
    _register_tools(app)
    result = app.tools["renforge_launch"](str(tmp_path))

    assert result["ok"] is True
    assert calls["kwargs"]["savedir"] == "existing"
    assert calls["kwargs"]["home"] == "existing"
    assert calls["kwargs"]["persistent"] == "existing"
    assert calls["kwargs"]["preferences"] == "existing"


def test_launch_tool_prefers_the_active_dashboard_process(tmp_path, monkeypatch) -> None:
    (tmp_path / "game").mkdir()
    pytest.importorskip("fastmcp", reason="fastmcp not installed")
    from fastmcp import Client

    from renforge import dashboard_client
    from renforge.tools import live

    calls = {}

    def fake_dashboard_launch(project_path: str, **kwargs):
        calls.update(
            project_path=project_path,
            version=kwargs.get("version"),
            warp=kwargs.get("warp"),
            editor=kwargs.get("editor"),
        )
        return {"ok": True, "ready": True, "status": "ready", "via": "dashboard"}

    monkeypatch.setattr(dashboard_client, "launch_game", fake_dashboard_launch)
    monkeypatch.setattr(
        live,
        "launch_game",
        lambda *_args, **_kwargs: pytest.fail("direct launch should not run"),
    )

    async def _call():
        async with Client(create_app()) as client:
            return await client.call_tool(
                "renforge_launch",
                {"project_path": str(tmp_path), "warp": "game/script.rpy:42", "editor": True},
            )

    result = asyncio.run(_call())
    payload = json.loads(next(block.text for block in result.content if block.type == "text"))
    assert payload["ok"] is True
    assert payload["ready"] is True
    assert payload["status"] == "ready"
    assert payload["via"] == "dashboard"
    assert calls == {
        "project_path": str(tmp_path),
        "version": "stable",
        "warp": "game/script.rpy:42",
        "editor": True,
    }


def test_stop_tool_prefers_the_active_dashboard_process(tmp_path, monkeypatch) -> None:
    from renforge import dashboard_client
    from renforge.tools import live

    calls = {}

    def fake_dashboard_stop(project_path: str):
        calls["project_path"] = project_path
        return {"ok": True, "was_running": True, "via": "dashboard"}

    monkeypatch.setattr(dashboard_client, "stop_game", fake_dashboard_stop)
    monkeypatch.setattr(
        live,
        "stop_game",
        lambda *_args, **_kwargs: pytest.fail("direct stop should not run"),
    )

    app = _ToolRegistry()
    _register_tools(app)
    result = app.tools["renforge_stop"](str(tmp_path))

    assert result == {
        "ok": True,
        "was_running": True,
        "via": "dashboard",
    }
    assert calls == {"project_path": str(tmp_path)}


def test_stop_tool_falls_back_when_dashboard_is_unavailable(tmp_path, monkeypatch) -> None:
    from renforge import dashboard_client
    from renforge.tools import live

    calls = {}
    monkeypatch.setattr(dashboard_client, "stop_game", lambda _project_path: None)

    def fake_direct_stop(project_path: str):
        calls["project_path"] = project_path
        return {"ok": True, "was_running": False, "files_deleted": []}

    monkeypatch.setattr(live, "stop_game", fake_direct_stop)

    app = _ToolRegistry()
    _register_tools(app)
    result = app.tools["renforge_stop"](str(tmp_path))

    assert result == {"ok": True, "was_running": False, "files_deleted": []}
    assert calls == {"project_path": str(tmp_path)}


def test_cancelled_dashboard_launch_stops_through_its_owner(tmp_path, monkeypatch) -> None:
    from renforge import dashboard_client
    from renforge.tools import live

    cancel_event = threading.Event()
    calls = {"stops": 0}

    def fake_dashboard_launch(*_args, **_kwargs):
        cancel_event.set()
        return {"ok": True, "via": "dashboard"}

    def fake_dashboard_stop(project_path: str):
        calls["stops"] += 1
        calls["project_path"] = project_path
        return {"ok": True, "via": "dashboard"}

    monkeypatch.setattr(dashboard_client, "launch_game", fake_dashboard_launch)
    monkeypatch.setattr(dashboard_client, "stop_game", fake_dashboard_stop)
    monkeypatch.setattr(
        live,
        "start_launch",
        lambda _project_path, launch, **_kwargs: launch(tmp_path.resolve(), cancel_event),
    )
    monkeypatch.setattr(
        live,
        "stop_external_game",
        lambda *_args, **_kwargs: pytest.fail("must stop through the dashboard owner"),
    )

    app = _ToolRegistry()
    _register_tools(app)
    result = app.tools["renforge_launch"](str(tmp_path))

    assert result["code"] == "LAUNCH_CANCELLED"
    assert calls == {"stops": 1, "project_path": str(tmp_path)}


@pytest.mark.parametrize(
    ("stop_result", "expected_code"),
    [
        (None, "DASHBOARD_STOP_UNAVAILABLE"),
        (
            {
                "ok": False,
                "code": "DASHBOARD_STOP_REJECTED",
                "error": "owner refused stop",
                "details": {"owner": "dashboard"},
            },
            "DASHBOARD_STOP_REJECTED",
        ),
    ],
)
def test_cancelled_dashboard_launch_reports_owner_stop_failure(
    tmp_path,
    monkeypatch,
    stop_result,
    expected_code,
) -> None:
    from renforge import dashboard_client
    from renforge.tools import live

    cancel_event = threading.Event()

    def fake_dashboard_launch(*_args, **_kwargs):
        cancel_event.set()
        return {"ok": True, "via": "dashboard"}

    monkeypatch.setattr(dashboard_client, "launch_game", fake_dashboard_launch)
    monkeypatch.setattr(
        dashboard_client,
        "stop_game",
        lambda _project_path: stop_result,
    )
    monkeypatch.setattr(
        live,
        "start_launch",
        lambda _project_path, launch, **_kwargs: launch(tmp_path.resolve(), cancel_event),
    )
    monkeypatch.setattr(
        live,
        "stop_external_game",
        lambda *_args, **_kwargs: pytest.fail("must not bypass dashboard ownership"),
    )

    app = _ToolRegistry()
    _register_tools(app)
    result = app.tools["renforge_launch"](str(tmp_path))

    assert result["ok"] is False
    assert result["ready"] is False
    assert result["code"] == expected_code
    assert result["launch_cancel_requested"] is True
    if stop_result is not None:
        assert result["error"] == "owner refused stop"
        assert result["details"] == {"owner": "dashboard"}


def test_cancelled_launch_propagates_external_lock_conflict(tmp_path, monkeypatch) -> None:
    from renforge import dashboard_client
    from renforge.tools import live

    cancel_event = threading.Event()

    def unavailable_dashboard(*_args, **_kwargs):
        cancel_event.set()
        return None

    monkeypatch.setattr(dashboard_client, "launch_game", unavailable_dashboard)
    monkeypatch.setattr(
        dashboard_client,
        "stop_game",
        lambda *_args, **_kwargs: pytest.fail("dashboard did not own this launch"),
    )
    monkeypatch.setattr(
        live,
        "start_launch",
        lambda _project_path, launch, **_kwargs: launch(tmp_path.resolve(), cancel_event),
    )
    monkeypatch.setattr(
        live,
        "stop_external_game",
        lambda _project_path: {
            "ok": False,
            "ready": False,
            "code": "BRIDGE_PROJECT_LOCKED",
            "phase": "acquiring_project_lock",
            "error": "another owner holds the lock",
            "details": {"owner": "external"},
        },
    )

    app = _ToolRegistry()
    _register_tools(app)
    result = app.tools["renforge_launch"](str(tmp_path))

    assert result["ok"] is False
    assert result["ready"] is False
    assert result["code"] == "BRIDGE_PROJECT_LOCKED"
    assert result["phase"] == "acquiring_project_lock"
    assert result["launch_cancel_requested"] is True
    assert result["details"] == {"owner": "external"}


def test_jump_tool_resolves_a_label_and_relaunches_at_it(tmp_path, monkeypatch) -> None:
    pytest.importorskip("fastmcp", reason="fastmcp not installed")
    from fastmcp import Client

    from renforge.tools import live

    game = tmp_path / "game"
    game.mkdir()
    (game / "script.rpy").write_text(
        "label start:\n    return\nlabel chapter_two:\n    return\n",
        encoding="utf-8",
    )
    calls = {}

    def fake_launch(project_path: str, version: str = "stable", warp: str | None = None, **_kwargs):
        calls.update(project_path=project_path, version=version, warp=warp)
        return {"ok": True, "current_label": "chapter_two"}

    monkeypatch.setattr(live, "launch_game", fake_launch)

    async def _call():
        async with Client(create_app()) as client:
            return await client.call_tool(
                "renforge_jump",
                {"project_path": str(tmp_path), "target": "chapter_two"},
            )

    result = asyncio.run(_call())
    assert result.is_error is False
    assert calls["project_path"] == str(tmp_path)
    assert calls["warp"].endswith("script.rpy:3")


def test_new_game_tool_relaunches_a_fresh_process_at_start(tmp_path, monkeypatch) -> None:
    pytest.importorskip("fastmcp", reason="fastmcp not installed")
    from fastmcp import Client

    from renforge.tools import live

    game = tmp_path / "game"
    game.mkdir()
    (game / "script.rpy").write_text("label start:\n    return\n", encoding="utf-8")
    calls = {}

    def fake_launch(project_path: str, version: str = "stable", warp: str | None = None, **_kwargs):
        calls.update(project_path=project_path, version=version, warp=warp)
        return {"ok": True, "current_label": "start"}

    monkeypatch.setattr(live, "launch_game", fake_launch)

    async def _call():
        async with Client(create_app()) as client:
            return await client.call_tool(
                "renforge_new_game",
                {"project_path": str(tmp_path)},
            )

    result = asyncio.run(_call())
    assert result.is_error is False
    assert calls == {
        "project_path": str(tmp_path),
        "version": "stable",
        "warp": "game/script.rpy:1",
    }


def test_control_tool_dispatches_runtime_action_and_documents_valid_actions(
    tmp_path, monkeypatch
) -> None:
    pytest.importorskip("fastmcp", reason="fastmcp not installed")
    from fastmcp import Client

    from renforge.tools import live

    calls = {}

    def fake_control(project_path: str, action: str, **kwargs):
        calls.update(project_path=project_path, action=action)
        calls["kwargs"] = kwargs
        return {"ok": True, "action": action}

    monkeypatch.setattr(live, "control", fake_control)
    app = create_app()

    async def _call():
        tools = await app.list_tools()
        async with Client(app) as client:
            result = await client.call_tool(
                "renforge_control",
                {"project_path": str(tmp_path), "action": "reload_script"},
            )
        return tools, result

    tools, result = asyncio.run(_call())
    payload = json.loads(next(block.text for block in result.content if block.type == "text"))
    description = next(tool.description for tool in tools if tool.name == "renforge_control")
    valid_actions = {
        "advance",
        "rollback",
        "toggle_skip",
        "toggle_auto",
        "toggle_afm",
        "game_menu",
        "hide_windows",
        "quick_save",
        "quick_load",
        "reload_script",
        "restart_interaction",
        "quit",
    }

    assert payload == {"ok": True, "action": "reload_script"}
    assert calls["project_path"] == str(tmp_path)
    assert calls["action"] == "reload_script"
    assert all(action in description for action in valid_actions)


def test_saves_tool_dispatches_grouped_save_action(tmp_path, monkeypatch) -> None:
    pytest.importorskip("fastmcp", reason="fastmcp not installed")
    from fastmcp import Client

    from renforge.tools import live

    calls = {}

    def fake_saves(project_path, action, slot=None, extra_info=None, regexp=None, slots=None, pattern=None):
        calls.update(
            project_path=project_path,
            action=action,
            slot=slot,
            extra_info=extra_info,
            regexp=regexp,
            slots=slots,
        )
        return {"ok": True, "slot": slot, "extra_info": extra_info}

    monkeypatch.setattr(live, "saves", fake_saves)

    async def _call():
        async with Client(create_app()) as client:
            return await client.call_tool(
                "renforge_saves",
                {
                    "project_path": str(tmp_path),
                    "action": "save",
                    "slot": "branch-a",
                    "extra_info": "before menu",
                },
            )

    result = asyncio.run(_call())
    payload = json.loads(next(block.text for block in result.content if block.type == "text"))

    assert payload == {"ok": True, "slot": "branch-a", "extra_info": "before menu"}
    assert calls == {
        "project_path": str(tmp_path),
        "action": "save",
        "slot": "branch-a",
        "extra_info": "before menu",
        "regexp": None,
        "slots": None,
    }


@pytest.mark.parametrize("action", ["list_user", "import"])
def test_saves_tool_forwards_pattern(tmp_path, monkeypatch, action) -> None:
    pytest.importorskip("fastmcp", reason="fastmcp not installed")
    from fastmcp import Client

    from renforge.tools import live

    calls = {}

    def fake_saves(project_path, action, slot=None, extra_info=None, regexp=None, slots=None, pattern=None):
        calls.update(action=action, pattern=pattern, regexp=regexp)
        return {"ok": True}

    monkeypatch.setattr(live, "saves", fake_saves)

    async def _call():
        async with Client(create_app()) as client:
            return await client.call_tool(
                "renforge_saves",
                {"project_path": str(tmp_path), "action": action, "pattern": "1-*"},
            )

    asyncio.run(_call())

    assert calls == {"action": action, "pattern": "1-*", "regexp": None}


def test_saves_tool_validates_action_and_required_slot(tmp_path) -> None:
    pytest.importorskip("fastmcp", reason="fastmcp not installed")
    from fastmcp import Client
    from renforge.tools import live

    async def _call(action, slot=None):
        async with Client(create_app()) as client:
            payload = {"project_path": str(tmp_path), "action": action}
            if slot is not None:
                payload["slot"] = slot
            return await client.call_tool("renforge_saves", payload, raise_on_error=False)

    invalid_action = asyncio.run(_call("archive"))
    missing_slot = asyncio.run(_call("save"))

    assert invalid_action.is_error is True
    assert "Input should be 'save', 'load', 'list', 'list_user' or 'import'" in invalid_action.content[0].text
    missing_payload = json.loads(next(block.text for block in missing_slot.content if block.type == "text"))
    assert missing_payload == {
        "ok": False,
        "error": "slot is required for action 'save'",
    }
    assert live.saves(str(tmp_path), "list", regexp=123) == {
        "ok": False,
        "error": "regexp must be a string",
    }


def test_saves_tool_dispatches_load_and_list_actions(tmp_path, monkeypatch) -> None:
    pytest.importorskip("fastmcp", reason="fastmcp not installed")
    from fastmcp import Client

    from renforge.tools import live

    calls = []

    def fake_saves(project_path, action, slot=None, extra_info=None, regexp=None, slots=None, pattern=None):
        calls.append(
            {
                "project_path": project_path,
                "action": action,
                "slot": slot,
                "extra_info": extra_info,
                "regexp": regexp,
                "slots": slots,
            }
        )
        return {"ok": True, "action": action}

    monkeypatch.setattr(live, "saves", fake_saves)

    async def _call(payload):
        async with Client(create_app()) as client:
            return await client.call_tool("renforge_saves", payload)

    load = asyncio.run(
        _call({"project_path": str(tmp_path), "action": "load", "slot": "branch-a"})
    )
    listed = asyncio.run(
        _call({"project_path": str(tmp_path), "action": "list", "regexp": "branch"})
    )

    assert json.loads(next(block.text for block in load.content if block.type == "text")) == {
        "ok": True,
        "action": "load",
    }
    assert json.loads(next(block.text for block in listed.content if block.type == "text")) == {
        "ok": True,
        "action": "list",
    }
    assert calls == [
        {
            "project_path": str(tmp_path),
            "action": "load",
            "slot": "branch-a",
            "extra_info": None,
            "regexp": None,
            "slots": None,
        },
        {
            "project_path": str(tmp_path),
            "action": "list",
            "slot": None,
            "extra_info": None,
            "regexp": "branch",
            "slots": None,
        },
    ]


def test_saves_tool_is_listed_with_grouped_actions() -> None:
    pytest.importorskip("fastmcp", reason="fastmcp not installed")

    tools = asyncio.run(create_app().list_tools())
    tool = next(tool for tool in tools if tool.name == "renforge_saves")

    assert all(action in tool.description for action in ("save", "load", "list", "list_user", "import"))
    assert tool.parameters["required"] == ["project_path", "action"]
    assert set(tool.parameters["properties"]) == {
        "project_path",
        "action",
        "slot",
        "extra_info",
        "regexp",
        "slots",
        "pattern",
        "authorize",
    }


def test_send_input_tool_dispatches_one_grouped_mode(tmp_path, monkeypatch) -> None:
    pytest.importorskip("fastmcp", reason="fastmcp not installed")
    from fastmcp import Client
    from renforge.tools import live

    calls = []

    def fake_send_input(project_path, **kwargs):
        calls.append((project_path, kwargs))
        return {"ok": True, "mode": "text", "characters": 4, "submitted": True}

    monkeypatch.setattr(live, "send_input", fake_send_input)

    async def _call():
        async with Client(create_app()) as client:
            return await client.call_tool(
                "renforge_send_input",
                {"project_path": str(tmp_path), "text": "Alex", "submit": True},
            )

    result = asyncio.run(_call())
    payload = json.loads(next(block.text for block in result.content if block.type == "text"))
    assert payload == {"ok": True, "mode": "text", "characters": 4, "submitted": True}
    assert calls == [
        (
            str(tmp_path),
            {"text": "Alex", "key": None, "scroll": None, "submit": True},
        )
    ]


def test_send_input_tool_schema_exposes_text_key_and_scroll() -> None:
    pytest.importorskip("fastmcp", reason="fastmcp not installed")

    tools = asyncio.run(create_app().list_tools())
    tool = next(tool for tool in tools if tool.name == "renforge_send_input")
    assert tool.parameters["required"] == ["project_path"]
    assert set(tool.parameters["properties"]) == {
        "project_path",
        "text",
        "key",
        "scroll",
        "submit",
    }


def test_get_errors_tool_dispatches_since_cursor(tmp_path, monkeypatch) -> None:
    pytest.importorskip("fastmcp", reason="fastmcp not installed")
    from fastmcp import Client

    from renforge.tools import live

    calls = {}

    def fake_get_errors(project_path, since=0):
        calls.update(project_path=project_path, since=since)
        return {"ok": True, "events": [], "cursor": since}

    monkeypatch.setattr(live, "get_errors", fake_get_errors)

    async def _call():
        async with Client(create_app()) as client:
            return await client.call_tool(
                "renforge_get_errors",
                {"project_path": str(tmp_path), "since": 7},
            )

    result = asyncio.run(_call())
    payload = json.loads(next(block.text for block in result.content if block.type == "text"))

    assert payload == {"ok": True, "events": [], "cursor": 7}
    assert calls == {"project_path": str(tmp_path), "since": 7}


def test_wait_until_tool_dispatches_condition(tmp_path, monkeypatch) -> None:
    pytest.importorskip("fastmcp", reason="fastmcp not installed")
    from fastmcp import Client

    from renforge.tools import live

    calls = {}

    def fake_wait_until(project_path, **kwargs):
        calls.update(project_path=project_path, **kwargs)
        return {
            "ok": True,
            "matched": "label",
            "elapsed": 0.1,
            "state": {"current_label": "chapter_two"},
        }

    monkeypatch.setattr(live, "wait_until", fake_wait_until)

    async def _call():
        async with Client(create_app()) as client:
            return await client.call_tool(
                "renforge_wait_until",
                {
                    "project_path": str(tmp_path),
                    "label": "chapter_two",
                    "timeout": 5,
                    "interval": 0.1,
                },
            )

    result = asyncio.run(_call())
    payload = json.loads(next(block.text for block in result.content if block.type == "text"))

    assert payload == {
        "ok": True,
        "matched": "label",
        "elapsed": 0.1,
        "state": {"current_label": "chapter_two"},
    }
    assert calls["project_path"] == str(tmp_path)
    assert calls["label"] == "chapter_two"
    assert calls["screen"] is None
    assert calls["expr"] is None
    assert calls["timeout"] == 5.0
    assert calls["interval"] == 0.1
    assert calls["state_profile"] == "interaction"


def test_wait_until_tool_enforces_maximum_timeout(tmp_path, monkeypatch) -> None:
    pytest.importorskip("fastmcp", reason="fastmcp not installed")
    from fastmcp import Client

    from renforge.tools import live

    monkeypatch.setattr(
        live,
        "wait_until",
        lambda **_kwargs: pytest.fail("wait_until should not run above server maximum"),
    )

    async def _call():
        async with Client(create_app()) as client:
            return await client.call_tool(
                "renforge_wait_until",
                {"project_path": str(tmp_path), "label": "chapter_two", "timeout": 121},
                raise_on_error=False,
            )

    result = asyncio.run(_call())
    assert result.is_error is True
    assert "less than or equal to 120" in result.content[0].text


def test_wait_until_tool_schema_covers_conditions_and_polling() -> None:
    pytest.importorskip("fastmcp", reason="fastmcp not installed")

    tools = asyncio.run(create_app().list_tools())
    tool = next(tool for tool in tools if tool.name == "renforge_wait_until")

    assert all(condition in tool.description for condition in ("label", "screen", "expr"))
    assert tool.parameters["required"] == ["project_path"]
    assert {
        "project_path",
        "label",
        "screen",
        "expr",
        "timeout",
        "interval",
        "state_profile",
        "include",
        "max_depth",
        "max_items",
        "max_output_bytes",
    }.issubset(set(tool.parameters["properties"]))


def test_ui_tools_expose_semantic_elements_and_coordinate_guards(tmp_path, monkeypatch) -> None:
    pytest.importorskip("fastmcp", reason="fastmcp not installed")
    from fastmcp import Client

    from renforge.tools import live

    calls = {}
    monkeypatch.setattr(
        live,
        "list_ui_elements",
        lambda path, **kwargs: {
            "ok": True,
            "frame_id": "frame-1",
            "elements": [
                {
                    "id": "save-1",
                    "text": "Save",
                    "type": "button",
                    "bounds": {"x": 20, "y": 30, "width": 80, "height": 40},
                }
            ],
            "screen": "quick_menu",
        },
    )

    def fake_click_element(path, **kwargs):
        calls["element"] = (path, kwargs)
        return {"ok": True, "id": kwargs["element_id"], "x": 60, "y": 50}

    def fake_hover_element(path, **kwargs):
        calls["hover"] = (path, kwargs)
        return {"ok": True, "hovered": True, "id": kwargs["element_id"], "x": 60, "y": 50}

    def fake_get_ui_element_bounds(path, **kwargs):
        calls["bounds"] = (path, kwargs)
        return {
            "ok": True,
            "focus_bounds": {"x": 20, "y": 30, "width": 80, "height": 40},
            "painted_bounds": {"x": 24, "y": 34, "width": 72, "height": 32},
            "painted_bounds_available": True,
        }

    def fake_click_at(path, x, y, **kwargs):
        calls["at"] = (path, x, y, kwargs)
        return {"ok": True, "x": x, "y": y}

    monkeypatch.setattr(live, "click_element", fake_click_element)
    monkeypatch.setattr(live, "hover_element", fake_hover_element)
    monkeypatch.setattr(live, "get_ui_element_bounds", fake_get_ui_element_bounds)
    monkeypatch.setattr(live, "click_at", fake_click_at)

    async def _call():
        async with Client(create_app()) as client:
            listed = await client.call_tool(
                "renforge_list_ui_elements", {"project_path": str(tmp_path)}
            )
            clicked = await client.call_tool(
                "renforge_click_element",
                {
                    "project_path": str(tmp_path),
                    "element_id": "save-1",
                    "expected_frame_id": "frame-1",
                },
            )
            bounds = await client.call_tool(
                "renforge_get_ui_element_bounds",
                {
                    "project_path": str(tmp_path),
                    "element_id": "save-1",
                    "expected_frame_id": "frame-1",
                },
            )
            by_coord = await client.call_tool(
                "renforge_click_at",
                {
                    "project_path": str(tmp_path),
                    "x": 60,
                    "y": 50,
                    "expected_frame_id": "frame-1",
                    "expected_state": {"menu": True},
                    "coordinate_space": "screenshot",
                },
            )
            return listed, clicked, bounds, by_coord

    listed, clicked, bounds, by_coord = asyncio.run(_call())
    listed_payload = json.loads(next(block.text for block in listed.content if block.type == "text"))
    clicked_payload = json.loads(next(block.text for block in clicked.content if block.type == "text"))
    bounds_payload = json.loads(next(block.text for block in bounds.content if block.type == "text"))
    coord_payload = json.loads(next(block.text for block in by_coord.content if block.type == "text"))
    assert listed_payload["elements"][0]["bounds"]["width"] == 80
    assert clicked_payload == {"ok": True, "id": "save-1", "x": 60, "y": 50}
    assert bounds_payload["painted_bounds_available"] is True
    assert bounds_payload["painted_bounds"]["width"] == 72
    assert coord_payload == {"ok": True, "x": 60, "y": 50}
    assert calls["element"][1]["expected_frame_id"] == "frame-1"
    assert calls["bounds"][1]["expected_frame_id"] == "frame-1"
    assert calls["at"][3]["expected_state"] == {"menu": True}
    assert calls["at"][3]["coordinate_space"] == "screenshot"


def test_find_image_on_screen_returns_template_bounds(tmp_path, monkeypatch) -> None:
    pytest.importorskip("fastmcp", reason="fastmcp not installed")
    image_module = pytest.importorskip("PIL.Image", reason="Pillow not installed")
    from fastmcp import Client

    from renforge.tools import live

    game = tmp_path / "game"
    game.mkdir()
    source = image_module.new("RGB", (30, 20), "black")
    source.paste("white", (12, 6, 16, 10))
    encoded = io.BytesIO()
    source.save(encoded, format="PNG")
    template = game / "save.png"
    source.crop((12, 6, 16, 10)).save(template)
    monkeypatch.setattr(live, "screenshot_png", lambda _path: encoded.getvalue())

    async def _call():
        async with Client(create_app()) as client:
            return await client.call_tool(
                "renforge_find_image_on_screen",
                {"project_path": str(tmp_path), "template_path": "game/save.png"},
            )

    result = asyncio.run(_call())
    payload = json.loads(next(block.text for block in result.content if block.type == "text"))
    assert payload["ok"] is True
    assert payload["matches"][0]["bounds"] == {
        "x": 12,
        "y": 6,
        "width": 4,
        "height": 4,
    }
    assert len(payload["frame_id"]) == 64
    assert payload["coordinate_space"] == "screenshot"
    assert payload["click_hint"]["expected_frame_id"] == payload["frame_id"]


def test_inspect_image_crops_and_zooms_without_external_scripts(tmp_path) -> None:
    pytest.importorskip("fastmcp", reason="fastmcp not installed")
    image_module = pytest.importorskip("PIL.Image", reason="Pillow not installed")
    from fastmcp import Client

    source = tmp_path / "mockup.png"
    image = image_module.new("RGB", (100, 60), "red")
    image.paste("blue", (50, 0, 100, 60))
    image.save(source)

    async def _call():
        async with Client(create_app()) as client:
            return await client.call_tool(
                "renforge_inspect_image",
                {
                    "image_path": str(source),
                    "crop_x": 50,
                    "crop_y": 0,
                    "crop_width": 50,
                    "crop_height": 60,
                    "scale": 2.0,
                },
            )

    result = asyncio.run(_call())
    block = next(block for block in result.content if block.type == "image")
    cropped = image_module.open(io.BytesIO(base64.b64decode(block.data)))
    assert cropped.size == (100, 120)
    assert cropped.getpixel((50, 60))[:3] == (0, 0, 255)


def test_screenshot_can_crop_and_zoom_the_live_frame(tmp_path, monkeypatch) -> None:
    pytest.importorskip("fastmcp", reason="fastmcp not installed")
    image_module = pytest.importorskip("PIL.Image", reason="Pillow not installed")
    from fastmcp import Client

    from renforge.tools import live

    source = image_module.new("RGB", (100, 60), "red")
    source.paste("blue", (50, 0, 100, 60))
    encoded = io.BytesIO()
    source.save(encoded, format="PNG")
    calls = {}

    def fake_screenshot(path: str, width: int = 0, height: int = 0) -> bytes:
        calls.update(path=path, width=width, height=height)
        return encoded.getvalue()

    monkeypatch.setattr(live, "screenshot_png", fake_screenshot)

    async def _call():
        async with Client(create_app()) as client:
            return await client.call_tool(
                "renforge_screenshot",
                {
                    "project_path": str(tmp_path),
                    "width": 100,
                    "height": 60,
                    "crop_x": 50,
                    "crop_y": 0,
                    "crop_width": 50,
                    "crop_height": 60,
                    "scale": 2.0,
                },
            )

    result = asyncio.run(_call())
    block = next(block for block in result.content if block.type == "image")
    cropped = image_module.open(io.BytesIO(base64.b64decode(block.data)))
    assert cropped.size == (100, 120)
    assert calls == {"path": str(tmp_path), "width": 100, "height": 60}


def test_screenshot_accepts_a_single_dimension(tmp_path, monkeypatch) -> None:
    pytest.importorskip("fastmcp", reason="fastmcp not installed")
    from fastmcp import Client

    from renforge.tools import live

    calls = {}

    def fake_screenshot(path: str, width: int = 0, height: int = 0) -> bytes:
        calls.update(width=width, height=height)
        return b"fake-png-bytes"

    monkeypatch.setattr(live, "screenshot_png", fake_screenshot)

    async def _call():
        async with Client(create_app()) as client:
            return await client.call_tool(
                "renforge_screenshot",
                {"project_path": str(tmp_path), "width": 1280},
            )

    result = asyncio.run(_call())
    # The bridge derives the missing height from the game's aspect ratio.
    assert any(block.type == "image" for block in result.content)
    assert calls == {"width": 1280, "height": 0}


def test_screenshot_rejects_negative_dimensions(tmp_path, monkeypatch) -> None:
    pytest.importorskip("fastmcp", reason="fastmcp not installed")
    from fastmcp import Client

    from renforge.tools import live

    monkeypatch.setattr(live, "screenshot_png", lambda *a, **k: b"fake-png-bytes")

    async def _call():
        async with Client(create_app()) as client:
            return await client.call_tool(
                "renforge_screenshot",
                {"project_path": str(tmp_path), "width": -1},
                raise_on_error=False,
            )

    result = asyncio.run(_call())
    assert result.is_error is True
    assert "greater than or equal to 0" in result.content[0].text


def test_find_references_tool_reports_unused_definitions(tmp_path) -> None:
    pytest.importorskip("fastmcp", reason="fastmcp not installed")
    from fastmcp import Client

    game = tmp_path / "game"
    game.mkdir()
    (game / "ui.rpy").write_text(
        'define DEAD_ICON = "images/dead.png"\n# DEAD_ICON\n',
        encoding="utf-8",
    )

    async def _call():
        async with Client(create_app()) as client:
            return await client.call_tool(
                "renforge_find_references",
                {"project_path": str(tmp_path), "symbol": "DEAD_ICON"},
            )

    result = asyncio.run(_call())
    payload = json.loads(next(block.text for block in result.content if block.type == "text"))
    assert payload["unused"] is True
    assert payload["definition_count"] == 1
    assert payload["reference_count"] == 0


def test_game_state_preserves_full_state_and_compact_tool_can_select_variables(tmp_path, monkeypatch) -> None:
    pytest.importorskip("fastmcp", reason="fastmcp not installed")
    from fastmcp import Client

    from renforge.tools import live

    monkeypatch.setattr(
        live,
        "game_state",
        lambda _path: {
            "ok": True,
            "current_label": "start",
            "showing_tags": ["bg"],
            "menu": False,
            "variables": {"score": 7, "player_name": "Rin", "ICON_UNUSED": "x.png"},
        },
    )

    async def _call():
        async with Client(create_app()) as client:
            full = await client.call_tool(
                "renforge_game_state",
                {"project_path": str(tmp_path)},
            )
            selected = await client.call_tool(
                "renforge_game_state_compact",
                {"project_path": str(tmp_path), "variable_names": ["score"]},
            )
            return full, selected

    full_result, selected_result = asyncio.run(_call())
    full = json.loads(next(block.text for block in full_result.content if block.type == "text"))
    selected = json.loads(next(block.text for block in selected_result.content if block.type == "text"))
    assert full["variables"] == {
        "score": 7,
        "player_name": "Rin",
        "ICON_UNUSED": "x.png",
    }
    assert selected["variable_count"] == 3
    assert selected["variables"] == {"score": 7}


def test_game_state_tool_dispatches_optional_metrics_and_audio_includes(tmp_path, monkeypatch) -> None:
    pytest.importorskip("fastmcp", reason="fastmcp not installed")
    from fastmcp import Client

    from renforge.tools import live

    calls = {}

    def fake_game_state(project_path, include=None):
        calls.update(project_path=project_path, include=include)
        return {"ok": True, "metrics": {"fps": 60.0}, "audio": {"channels": {}}}

    monkeypatch.setattr(live, "game_state", fake_game_state)

    async def _call():
        app = create_app()
        tools = await app.list_tools()
        async with Client(app) as client:
            result = await client.call_tool(
                "renforge_game_state",
                {"project_path": str(tmp_path), "include": ["metrics", "audio"]},
            )
        return tools, result

    tools, result = asyncio.run(_call())
    payload = json.loads(next(block.text for block in result.content if block.type == "text"))
    tool = next(tool for tool in tools if tool.name == "renforge_game_state")

    assert payload["metrics"]["fps"] == 60.0
    assert "audio" in payload
    assert calls == {"project_path": str(tmp_path), "include": ["metrics", "audio"]}
    assert tool.parameters["required"] == ["project_path"]
    assert "include" in tool.parameters["properties"]
    assert "metrics" in tool.description and "audio" in tool.description


def test_inspect_screen_tool_dispatches_name_and_documents_inactive_result(tmp_path, monkeypatch) -> None:
    pytest.importorskip("fastmcp", reason="fastmcp not installed")
    from fastmcp import Client

    from renforge.tools import live

    calls = {}

    def fake_inspect_screen(project_path, name):
        calls.update(project_path=project_path, name=name)
        return {"ok": True, "active": False, "name": name, "error": "screen not showing: %s" % name}

    monkeypatch.setattr(live, "inspect_screen", fake_inspect_screen)

    async def _call():
        app = create_app()
        tools = await app.list_tools()
        async with Client(app) as client:
            result = await client.call_tool(
                "renforge_inspect_screen",
                {"project_path": str(tmp_path), "name": "custom"},
            )
        return tools, result

    tools, result = asyncio.run(_call())
    payload = json.loads(next(block.text for block in result.content if block.type == "text"))
    tool = next(tool for tool in tools if tool.name == "renforge_inspect_screen")

    assert payload == {
        "ok": True,
        "active": False,
        "name": "custom",
        "error": "screen not showing: custom",
    }
    assert calls == {"project_path": str(tmp_path), "name": "custom"}
    assert tool.parameters["required"] == ["project_path", "name"]
    assert set(tool.parameters["properties"]) == {"project_path", "name"}


def test_screenshot_serializes_to_an_mcp_image_block(tmp_path, monkeypatch) -> None:
    fastmcp = pytest.importorskip("fastmcp", reason="fastmcp not installed")
    import base64

    from fastmcp import Client

    from renforge.tools import live

    monkeypatch.setattr(live, "screenshot_png", lambda path: b"fake-png-bytes")
    app = create_app()
    if isinstance(app, _FallbackServer):
        pytest.skip("MCP backend (mcp/fastmcp) not installed")

    async def _call():
        async with Client(app) as client:
            return await client.call_tool(
                "renforge_screenshot", {"project_path": str(tmp_path)}
            )

    result = asyncio.run(_call())
    image_blocks = [block for block in result.content if getattr(block, "type", None) == "image"]
    assert image_blocks, f"expected an image content block, got: {result.content!r}"
    assert image_blocks[0].mimeType == "image/png"
    assert base64.b64decode(image_blocks[0].data) == b"fake-png-bytes"


def test_placement_tools_forward_to_the_bridge_client(tmp_path, monkeypatch) -> None:
    pytest.importorskip("fastmcp", reason="fastmcp not installed")
    from fastmcp import Client

    from renforge.tools import live

    calls = {}

    def fake_bounds(path, tag, **kwargs):
        calls["bounds"] = (path, tag, kwargs)
        return {"ok": True, "tag": tag, "bounds": {"x": 400, "y": 300, "width": 200, "height": 400}}

    def fake_position(path, tag, **kwargs):
        calls["position"] = (path, tag, kwargs)
        return {"ok": True, "tag": tag, "bounds": {"x": 960, "y": 100, "width": 200, "height": 400}}

    monkeypatch.setattr(live, "get_displayable_bounds", fake_bounds)
    monkeypatch.setattr(live, "position_element", fake_position)

    async def _call():
        async with Client(create_app()) as client:
            bounds = await client.call_tool(
                "renforge_get_displayable_bounds",
                {"project_path": str(tmp_path), "tag": "eileen"},
            )
            moved = await client.call_tool(
                "renforge_position_element",
                {"project_path": str(tmp_path), "tag": "eileen", "xpos": 960, "xanchor": 0.5},
            )
            return bounds, moved

    bounds, moved = asyncio.run(_call())
    bounds_payload = json.loads(next(b.text for b in bounds.content if b.type == "text"))
    moved_payload = json.loads(next(b.text for b in moved.content if b.type == "text"))
    assert bounds_payload["bounds"]["x"] == 400
    assert moved_payload["bounds"]["x"] == 960
    assert calls["bounds"][1] == "eileen"
    assert calls["position"][2] == {"layer": None, "xpos": 960, "xanchor": 0.5}


def test_diff_screenshots_tool_reports_the_changed_region(tmp_path, monkeypatch) -> None:
    pytest.importorskip("fastmcp", reason="fastmcp not installed")
    image_module = pytest.importorskip("PIL.Image", reason="Pillow not installed")
    from fastmcp import Client

    from renforge.tools import live

    before = image_module.new("RGB", (120, 80), "black")
    before.save(tmp_path / "before.png")
    after = image_module.new("RGB", (120, 80), "black")
    after.paste("white", (20, 10, 40, 30))
    encoded = io.BytesIO()
    after.save(encoded, format="PNG")
    monkeypatch.setattr(live, "screenshot_png", lambda _path: encoded.getvalue())

    async def _call():
        async with Client(create_app()) as client:
            return await client.call_tool(
                "renforge_diff_screenshots",
                {"project_path": str(tmp_path), "before_path": "before.png"},
            )

    result = asyncio.run(_call())
    payload = json.loads(next(b.text for b in result.content if b.type == "text"))
    assert payload["changed"] is True
    assert payload["bounds"] == {"x": 20, "y": 10, "width": 20, "height": 20}


def test_screenshot_can_overlay_a_measurement_grid(tmp_path, monkeypatch) -> None:
    pytest.importorskip("fastmcp", reason="fastmcp not installed")
    image_module = pytest.importorskip("PIL.Image", reason="Pillow not installed")
    import base64

    from fastmcp import Client

    from renforge.tools import live

    frame = image_module.new("RGB", (200, 120), "navy")
    encoded = io.BytesIO()
    frame.save(encoded, format="PNG")
    monkeypatch.setattr(live, "screenshot_png", lambda *a, **k: encoded.getvalue())

    async def _call():
        async with Client(create_app()) as client:
            return await client.call_tool(
                "renforge_screenshot",
                {"project_path": str(tmp_path), "grid": 50, "rulers": True},
            )

    result = asyncio.run(_call())
    image_blocks = [b for b in result.content if getattr(b, "type", None) == "image"]
    assert image_blocks, f"expected an image block, got: {result.content!r}"
    annotated = image_module.open(io.BytesIO(base64.b64decode(image_blocks[0].data)))
    assert annotated.size == (200, 120)
    assert annotated.convert("RGB").getpixel((50, 60)) != (0, 0, 128)


def test_scene_tree_tool_dispatches_options(tmp_path, monkeypatch) -> None:
    pytest.importorskip("fastmcp", reason="fastmcp not installed")
    from fastmcp import Client

    from renforge.tools import live

    calls = {}

    def fake_scene_tree(project_path, **kwargs):
        calls.update(project_path=project_path, **kwargs)
        return {"ok": True, "nodes": [], "counts": {"perceived": 0, "returned": 0}}

    monkeypatch.setattr(live, "scene_tree", fake_scene_tree)

    async def _call():
        async with Client(create_app()) as client:
            return await client.call_tool(
                "renforge_scene_tree",
                {
                    "project_path": str(tmp_path),
                    "detail": "raw",
                    "include": ["color", "style"],
                    "format": "wireframe",
                },
            )

    result = asyncio.run(_call())
    payload = json.loads(next(block.text for block in result.content if block.type == "text"))
    assert payload["ok"] is True
    assert calls["project_path"] == str(tmp_path)
    assert calls["detail"] == "raw"
    assert calls["include"] == ["color", "style"]
    assert calls["format"] == "wireframe"
    assert calls["max_output_depth"] == 6
    assert calls["max_items"] == 50
    assert calls["max_output_bytes"] == 65_536


def test_measure_tool_dispatches_action_and_targets(tmp_path, monkeypatch) -> None:
    pytest.importorskip("fastmcp", reason="fastmcp not installed")
    from fastmcp import Client

    from renforge.tools import live

    calls = {}

    def fake_measure(project_path, *, action, targets, within=None, tolerance=None):
        calls.update(
            project_path=project_path,
            action=action,
            targets=targets,
            within=within,
            tolerance=tolerance,
        )
        return {"ok": True, "action": action, "result": {}}

    monkeypatch.setattr(live, "measure", fake_measure)

    async def _call():
        async with Client(create_app()) as client:
            return await client.call_tool(
                "renforge_measure",
                {
                    "project_path": str(tmp_path),
                    "action": "align",
                    "targets": ["a", "b"],
                    "tolerance": 2,
                },
            )

    result = asyncio.run(_call())
    payload = json.loads(next(block.text for block in result.content if block.type == "text"))
    assert payload["ok"] is True
    assert calls["action"] == "align"
    assert calls["targets"] == ["a", "b"]
    assert calls["tolerance"] == 2


def test_session_launch_keeps_its_own_home_even_when_isolation_defaults_to_existing(tmp_path, monkeypatch) -> None:
    """The policy calls a session launch isolated, so RENFORGE_ISOLATION=existing must not hand it the player's HOME."""
    pytest.importorskip("fastmcp", reason="fastmcp not installed")
    import threading
    from pathlib import Path

    from fastmcp import Client

    from renforge import dashboard_client
    from renforge.tools import live

    monkeypatch.setenv("RENFORGE_ISOLATION", "existing")
    monkeypatch.setenv("RENFORGE_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(dashboard_client, "launch_game", lambda *a, **k: None)
    captured: dict = {}
    monkeypatch.setattr(live, "launch_game", lambda *a, **k: captured.update(k) or {"ok": True})

    def fake_start_launch(project_path, launch, editor=True, requirements=None, **_kwargs):
        return launch(Path(project_path), threading.Event())

    monkeypatch.setattr(live, "start_launch", fake_start_launch)

    async def _call():
        async with Client(create_app()) as client:
            return await client.call_tool("renforge_session", {"project_path": str(tmp_path), "action": "launch"})

    asyncio.run(_call())
    assert captured["home"] == "temporary"
    assert captured["editor"] is False


def test_session_launch_refuses_a_saves_folder_linked_out_of_the_state_folder(tmp_path, monkeypatch) -> None:
    """The policy lets a session launch through as isolated, so a planted link must not start it on the player's saves."""
    import os

    from renforge.save_isolation import host_renpy_root
    from renforge.tools import live

    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("APPDATA", str(home / "AppData" / "Roaming"))
    monkeypatch.setenv("RENFORGE_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("RENFORGE_POLICY", "enforce")
    project = tmp_path / "project"
    (project / "game").mkdir(parents=True)
    player = host_renpy_root(home=home) / "a-game"
    player.mkdir(parents=True)
    state_saves = project_state_dir(project) / "saves"
    state_saves.parent.mkdir(parents=True)
    try:
        os.symlink(player, state_saves, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable")
    started: list = []
    monkeypatch.setattr(live, "start_launch", lambda *a, **k: started.append(k) or {"ok": True, "ready": True})
    app = _ToolRegistry()
    _register_tools(app)

    refused = app.tools["renforge_session"](str(project), "launch")

    assert refused["ok"] is False and refused["code"] == "SESSION_NOT_ISOLATED", refused
    assert started == []

    # The same launch on the state folder's own saves goes ahead.
    state_saves.unlink()
    assert app.tools["renforge_session"](str(project), "launch")["ok"] is True
    assert len(started) == 1 and started[0]["requirements"]["isolated"] is True
    assert state_saves.is_dir() and not state_saves.is_symlink()


def test_session_launch_refuses_a_saves_folder_linked_inside_the_state_folder(tmp_path, monkeypatch) -> None:
    """A link to another folder of the state tree is still a link: it must not choose where the session saves."""
    import os

    from renforge.tools import live

    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("RENFORGE_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("RENFORGE_POLICY", "enforce")
    project = tmp_path / "project"
    (project / "game").mkdir(parents=True)
    other = tmp_path / "state" / "projects" / "another-project" / "saves"
    other.mkdir(parents=True)
    state_saves = project_state_dir(project) / "saves"
    state_saves.parent.mkdir(parents=True)
    try:
        os.symlink(other, state_saves, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable")
    started: list = []
    monkeypatch.setattr(live, "start_launch", lambda *a, **k: started.append(k) or {"ok": True, "ready": True})
    app = _ToolRegistry()
    _register_tools(app)

    refused = app.tools["renforge_session"](str(project), "launch")

    assert refused["ok"] is False and refused["code"] == "SESSION_NOT_ISOLATED", refused
    assert started == []


@pytest.mark.parametrize(("launched", "says_label"), [
    ({"ok": True, "ready": True, "already_running": False}, True),
    ({"ok": True, "ready": False, "status": "starting"}, True),
    ({"ok": True, "ready": True, "already_running": True}, False),
    ({"ok": False, "code": "LAUNCH_IN_PROGRESS"}, False),
])
def test_session_launch_reports_a_start_label_only_for_a_game_it_started(
        tmp_path, monkeypatch, launched, says_label) -> None:
    """A reused or refused launch never went to the label."""
    from renforge.tools import live

    monkeypatch.setenv("RENFORGE_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(live, "start_launch", lambda *_a, **_k: dict(launched))
    app = _ToolRegistry()
    _register_tools(app)

    result = app.tools["renforge_session"](str(tmp_path), "launch", label="chapter_two")

    assert ("start_label" in result) is says_label


def _saves_tool(monkeypatch, import_result):
    from renforge import dashboard_client
    from renforge.tools import live

    calls = {"import_saves": [], "saves": []}

    def fake_import(project_path, **kwargs):
        calls["import_saves"].append((project_path, kwargs))
        return import_result

    def fake_saves(project_path, action, **kwargs):
        calls["saves"].append((project_path, action, kwargs))
        return {"ok": False, "code": "NO_ISOLATED_SESSION", "local": True}

    monkeypatch.setattr(dashboard_client, "import_saves", fake_import)
    monkeypatch.setattr(live, "saves", fake_saves)
    app = _ToolRegistry()
    _register_tools(app)
    return app.tools["renforge_saves"], calls, live


def test_saves_import_delegates_when_this_process_owns_no_session(tmp_path, monkeypatch) -> None:
    (tmp_path / "game").mkdir()
    answer = {"ok": True, "via": "dashboard"}
    tool, calls, _live = _saves_tool(monkeypatch, answer)

    result = tool(str(tmp_path), "import", slots=["1-1"])

    assert result == answer
    assert calls["import_saves"] == [
        (str(tmp_path), {"slot": None, "slots": ["1-1"], "pattern": None})
    ]
    assert calls["saves"] == []


def test_saves_import_uses_the_owned_session_before_the_dashboard(tmp_path, monkeypatch) -> None:
    (tmp_path / "game").mkdir()
    tool, calls, live = _saves_tool(monkeypatch, {"ok": True, "via": "dashboard"})
    monkeypatch.setattr(live, "_SESSIONS", {live._key(tmp_path): object()})

    result = tool(str(tmp_path), "import", slots=["1-1"])

    assert result["local"] is True
    assert calls["import_saves"] == []
    assert [c[1] for c in calls["saves"]] == ["import"]


def test_saves_import_without_dashboard_reports_no_isolated_session(tmp_path, monkeypatch) -> None:
    (tmp_path / "game").mkdir()
    from renforge.tools import live as real_live

    original_saves = real_live.saves
    tool, calls, live = _saves_tool(monkeypatch, None)
    monkeypatch.setattr(live, "saves", original_saves)
    monkeypatch.setattr(live, "_SESSIONS", {})

    result = tool(str(tmp_path), "import", slots=["1-1"])

    assert len(calls["import_saves"]) == 1
    assert result["ok"] is False
    assert result["code"] == "NO_ISOLATED_SESSION"


def test_saves_import_dashboard_answer_is_final(tmp_path, monkeypatch) -> None:
    (tmp_path / "game").mkdir()
    answer = {"ok": False, "code": "NO_ISOLATED_SESSION", "via": "dashboard"}
    tool, calls, _live = _saves_tool(monkeypatch, answer)

    result = tool(str(tmp_path), "import", slots=["1-1"])

    assert result == answer
    assert calls["saves"] == []


def test_saves_import_extra_info_never_reaches_the_dashboard(tmp_path, monkeypatch) -> None:
    (tmp_path / "game").mkdir()
    from renforge.tools import live as real_live

    original_saves = real_live.saves
    tool, calls, live = _saves_tool(monkeypatch, {"ok": True, "via": "dashboard"})
    monkeypatch.setattr(live, "saves", original_saves)

    result = tool(str(tmp_path), "import", slots=["1-1"], extra_info="x")

    assert result["ok"] is False
    assert result["error"] == "extra_info is only valid for action 'save'"
    assert calls["import_saves"] == []
    for action in ("load", "list", "list_user", "import"):
        slot = "1-1" if action in ("load", "import") else None
        error = real_live.saves_argument_error(
            action, slot=slot, extra_info="x", regexp=None, slots=None, pattern=None
        )
        expected = {"ok": False, "error": "extra_info is only valid for action 'save'"}
        if action == "import":
            expected["code"] = "SAVE_IMPORT_INVALID"
        assert error == expected, action


def test_saves_import_argument_errors_never_reach_the_dashboard(tmp_path, monkeypatch) -> None:
    (tmp_path / "game").mkdir()
    from renforge.tools import live as real_live

    original_saves = real_live.saves
    tool, calls, live = _saves_tool(monkeypatch, {"ok": True, "via": "dashboard"})
    monkeypatch.setattr(live, "saves", original_saves)

    result = tool(str(tmp_path), "import", regexp="x")

    assert result["ok"] is False
    assert "pattern" in result["error"] and "regexp" in result["error"]
    assert calls["import_saves"] == []


@pytest.mark.parametrize("tool", ["renforge_jump", "renforge_new_game"])
def test_relaunch_tools_leave_unset_isolation_to_the_owning_process(
    tmp_path, monkeypatch, tool
) -> None:
    # The process that owns the running game decides whether an all-auto
    # jump inherits its isolation, so the wrapper must not resolve "auto"
    # to the player's saves first, locally or for a dashboard.
    from renforge import dashboard_client
    from renforge.tools import live

    game = tmp_path / "game"
    game.mkdir()
    (game / "script.rpy").write_text("label start:\n    return\nlabel other:\n    return\n", encoding="utf-8")
    knobs = ("savedir", "home", "persistent", "preferences")
    arguments = {"target": "other"} if tool == "renforge_jump" else {}

    seen: dict = {}
    monkeypatch.setattr(dashboard_client, "launch_game", lambda *_a, **_k: None)

    def fake_launch(project_path: str, version: str = "stable", warp: str | None = None, **kwargs):
        seen["local"] = {k: kwargs.get(k) for k in knobs}
        return {"ok": True, "ready": True, "already_running": False, "editor": True}

    monkeypatch.setattr(live, "launch_game", fake_launch)
    app = _ToolRegistry()
    _register_tools(app)
    assert app.tools[tool](str(tmp_path), **arguments)["ok"] is True
    assert seen["local"] == dict.fromkeys(knobs)

    def fake_dashboard(project_path: str, **kwargs):
        seen["dashboard"] = {k: kwargs.get(k) for k in knobs}
        return {"ok": True, "ready": True, "via": "dashboard", "editor": True}

    monkeypatch.setattr(dashboard_client, "launch_game", fake_dashboard)
    assert app.tools[tool](str(tmp_path), **arguments)["ok"] is True
    assert seen["dashboard"] == dict.fromkeys(knobs)

    explicit = app.tools[tool](str(tmp_path), savedir="temporary", **arguments)
    assert explicit["ok"] is True
    assert seen["dashboard"] == {
        "savedir": "temporary", "home": "temporary", "persistent": "empty", "preferences": "empty"
    }


@pytest.mark.parametrize("tool", ["renforge_jump", "renforge_new_game"])
def test_relaunch_tools_keep_this_servers_isolation_for_a_dashboard(tmp_path, monkeypatch, tool) -> None:
    # With RENFORGE_ISOLATION=temporary on this server, an all-auto relaunch
    # must stay isolated even when a dashboard started without it owns the
    # game: the owner may only strengthen isolation, never drop it.
    from renforge import dashboard_client
    from renforge.tools import live

    game = tmp_path / "game"
    game.mkdir()
    (game / "script.rpy").write_text("label start:\n    return\nlabel other:\n    return\n", encoding="utf-8")
    knobs = ("savedir", "home", "persistent", "preferences")
    arguments = {"target": "other"} if tool == "renforge_jump" else {}
    monkeypatch.setenv("RENFORGE_ISOLATION", "temporary")
    seen: dict = {}

    def fake_dashboard(project_path: str, **kwargs):
        seen["dashboard"] = {k: kwargs.get(k) for k in knobs}
        return {"ok": True, "ready": True, "via": "dashboard", "editor": True}

    monkeypatch.setattr(dashboard_client, "launch_game", fake_dashboard)
    monkeypatch.setattr(live, "launch_game", lambda *_a, **_k: pytest.fail("ran locally"))
    app = _ToolRegistry()
    _register_tools(app)

    assert app.tools[tool](str(tmp_path), **arguments)["ok"] is True
    assert seen["dashboard"] == {
        "savedir": "temporary", "home": "temporary", "persistent": "empty", "preferences": "empty"
    }
