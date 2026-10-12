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
