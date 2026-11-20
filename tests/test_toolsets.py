import asyncio

import pytest

from renforge.tool_definitions import TOOL_DEFINITIONS
from renforge.toolsets import TOOLSETS, allowed_tools


def test_every_tool_is_in_exactly_one_toolset():
    assigned = [name for names in TOOLSETS.values() for name in names]
    assert sorted(assigned) == sorted(TOOL_DEFINITIONS)


def test_default_lists_every_tool():
    assert allowed_tools(TOOL_DEFINITIONS, {}) == set(TOOL_DEFINITIONS)
    assert allowed_tools(TOOL_DEFINITIONS, {"RENFORGE_TOOLSETS": "all"}) == set(TOOL_DEFINITIONS)


def test_groups_combine_and_unknown_names_fail():
    env = {"RENFORGE_TOOLSETS": "play, docs"}
    assert allowed_tools(TOOL_DEFINITIONS, env) == set(TOOLSETS["play"]) | set(TOOLSETS["docs"])
    with pytest.raises(RuntimeError, match="plya"):
        allowed_tools(TOOL_DEFINITIONS, {"RENFORGE_TOOLSETS": "plya"})


def test_server_lists_only_the_play_loop(monkeypatch):
    monkeypatch.setenv("RENFORGE_TOOLSETS", "play")
    from renforge.server import create_app

    listed = {tool.name for tool in asyncio.run(create_app().list_tools())}
    assert listed == set(TOOLSETS["play"])


def test_only_servers_with_a_code_running_tool_let_games_run_code():
    from renforge.toolsets import bridge_eval_allowed

    assert bridge_eval_allowed({}) is True
    assert bridge_eval_allowed({"RENFORGE_TOOLSETS": "all"}) is True
    assert bridge_eval_allowed({"RENFORGE_TOOLSETS": "play, debug"}) is True
    assert bridge_eval_allowed({"RENFORGE_TOOLSETS": "play,editor"}) is True
    assert bridge_eval_allowed({"RENFORGE_TOOLSETS": "play"}) is False
    assert bridge_eval_allowed({"RENFORGE_TOOLSETS": "play, live, docs"}) is False
    # Only the groups above and `mod` run code; `play` alone never does.
    assert "renforge_mod" in TOOLSETS["mod"] and "renforge_mod" not in TOOLSETS["play"]
    # Every tool that evaluates code is in one of those groups.
    for tool in ("renforge_eval", "renforge_set_var", "renforge_wait_until", "renforge_run_scenario", "renforge_editor"):
        assert any(tool in TOOLSETS[group] for group in ("debug", "editor"))


def test_a_play_only_server_launches_games_that_refuse_eval(monkeypatch, tmp_path):
    from renforge.tools import live

    seen = []

    def fake_launch(_sdk, _project, **kwargs):
        seen.append(dict(kwargs.get("extra_env") or {}))
        raise live.LaunchError("STOP_HERE", "stop", phase="test")

    (tmp_path / "game").mkdir()
    monkeypatch.setattr(live, "launch_with_bridge", fake_launch)
    monkeypatch.setattr(live, "get_or_install_sdk", lambda *a, **k: object())
    for toolsets in ("play", "play,debug"):
        monkeypatch.setenv("RENFORGE_TOOLSETS", toolsets)
        live.launch_game(str(tmp_path), editor=False)
    assert [env.get("RENFORGE_BRIDGE_EVAL") for env in seen] == ["off", None]


def test_info_and_instructions_name_only_the_listed_tools(monkeypatch):
    """A play-only server never points an agent at tools it does not list."""
    pytest.importorskip("fastmcp", reason="fastmcp not installed")
    from fastmcp import Client

    from renforge.server import _instructions, create_app

    monkeypatch.setenv("RENFORGE_TOOLSETS", "play")

    async def info():
        async with Client(create_app()) as client:
            return (await client.call_tool("renforge_info", {})).structured_content

    payload = asyncio.run(info())
    assert payload["toolsets"] == ["play"]
    assert "live_editor" not in payload
    assert any("renforge_act" in step for step in payload["play"]["loop"])

    play_only = _instructions(["play"])
    assert "renforge_act" in play_only and "forward" in play_only
    assert "renforge_mod" not in play_only and "toolsets keep the earlier tools" not in play_only
    everything = _instructions(list(TOOLSETS))
    assert "renforge_mod" in everything and "Live Editor" in everything


@pytest.mark.parametrize(
    ("toolsets", "listed"),
    [(["editor"], False), (["play", "editor"], False), (["live"], False), (["editor", "live"], True)],
)
def test_instructions_name_the_live_editor_only_when_info_does(toolsets, listed):
    """The instructions use renforge_info's rule: the workflow needs both editor and live."""
    from renforge.server import _instructions

    assert ("Live Editor" in _instructions(toolsets)) is listed


@pytest.mark.parametrize(
    ("toolsets", "listed"),
    [("play,editor", False), ("live,project", False), ("editor,live,project", True), ("all", True)],
)
def test_info_lists_the_live_editor_only_when_its_workflow_tools_are_listed(monkeypatch, toolsets, listed):
    """The live_editor block names renforge_launch & co. (live) and renforge_editor (editor)."""
    pytest.importorskip("fastmcp", reason="fastmcp not installed")
    from fastmcp import Client

    from renforge.server import create_app
    from renforge.toolsets import allowed_tools

    monkeypatch.setenv("RENFORGE_TOOLSETS", toolsets)
    tool = "renforge_info" if "play" in toolsets or toolsets == "all" else "renforge_context"

    async def info():
        async with Client(create_app()) as client:
            return (await client.call_tool(tool, {})).structured_content

    payload = asyncio.run(info())
    assert ("live_editor" in payload) is listed
    if listed:
        everything = {name for names in TOOLSETS.values() for name in names}
        available = allowed_tools(everything)
        block = payload["live_editor"]
        assert block["launch_tool"] in available
        assert set(block["agent_workflow"]) - {"renforge_info"} <= available


@pytest.mark.parametrize("toolsets", ["live,editor", "debug", "project", "mod"])
def test_renforge_info_is_listed_without_the_play_group(toolsets):
    # Its description says "Call this first", and so do the server instructions.
    listed = allowed_tools(TOOL_DEFINITIONS, {"RENFORGE_TOOLSETS": toolsets})
    assert "renforge_info" in listed
    assert "renforge_session" not in listed


@pytest.mark.parametrize("value", [",", " , ", "Play", "play,plya"])
def test_every_reader_of_the_variable_rejects_the_same_values(value):
    from renforge.toolsets import bridge_eval_allowed, enabled_toolsets

    env = {"RENFORGE_TOOLSETS": value}
    with pytest.raises(RuntimeError):
        allowed_tools(TOOL_DEFINITIONS, env)
    with pytest.raises(RuntimeError):
        enabled_toolsets(env)
    # Launching games fails closed rather than enabling eval for a server
    # whose tool list could not be worked out.
    assert bridge_eval_allowed(env) is False


def test_enabled_toolsets_matches_the_listed_tools():
    from renforge.toolsets import enabled_toolsets

    env = {"RENFORGE_TOOLSETS": "docs, play"}
    assert enabled_toolsets(env) == ["play", "docs"]
    assert enabled_toolsets({"RENFORGE_TOOLSETS": "play, all"}) == list(TOOLSETS)


def test_image_shift_estimate_is_listed_with_the_capture_tools():
    listed = allowed_tools(TOOL_DEFINITIONS, {"RENFORGE_TOOLSETS": "live"})
    assert {"renforge_capture_screenshot", "renforge_estimate_translation"} <= listed
