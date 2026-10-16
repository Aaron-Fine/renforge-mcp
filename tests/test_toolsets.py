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
