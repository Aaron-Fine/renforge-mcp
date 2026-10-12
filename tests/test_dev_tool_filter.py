"""Fork-only tool filter (remove with renforge/dev_tool_filter.py)."""

import asyncio

import pytest

from renforge.dev_tool_filter import PROFILES, allowed_tools
from renforge.tool_definitions import TOOL_DEFINITIONS


def test_unset_lists_every_tool():
    assert allowed_tools(TOOL_DEFINITIONS, {}) == set(TOOL_DEFINITIONS)
    assert allowed_tools(TOOL_DEFINITIONS, {"RENFORGE_TOOLS": "all"}) == set(TOOL_DEFINITIONS)


def test_profiles_names_and_exclusions_combine():
    env = {"RENFORGE_TOOLS": "play, screenshot", "RENFORGE_TOOLS_EXCLUDE": "renforge_mod"}
    expected = set(PROFILES["play"]) | {"renforge_screenshot"}
    assert allowed_tools(TOOL_DEFINITIONS, env) == expected - {"renforge_mod"}


def test_unknown_names_fail_loudly():
    with pytest.raises(RuntimeError, match="observ"):
        allowed_tools(TOOL_DEFINITIONS, {"RENFORGE_TOOLS": "play,observ"})


def test_server_lists_only_the_allowed_tools(monkeypatch):
    monkeypatch.setenv("RENFORGE_TOOLS", "play")
    from renforge.server import create_app

    app = create_app()
    listed = {tool.name for tool in asyncio.run(app.list_tools())}
    assert listed == set(PROFILES["play"])
