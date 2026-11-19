"""Tool groups the server can list, chosen with ``RENFORGE_TOOLSETS``.

``play`` is the canonical agent loop: one tool per task. ``mod`` adds
``renforge_mod``, which writes and reloads game code. The other groups keep
every earlier capability available for existing clients, scripts, and the
Live Editor, and are listed unless the server is limited to fewer groups.

``RENFORGE_TOOLSETS``: comma-separated group names, or ``all`` (the default).
Unknown names, or a value that names no group, stop the server at startup so
a typo cannot hide tools. ``renforge_info`` is listed whatever the selection:
every workflow starts with it.
"""

from __future__ import annotations

import os
from collections.abc import Iterable, Mapping

TOOLSETS_ENV = "RENFORGE_TOOLSETS"

TOOLSETS: dict[str, tuple[str, ...]] = {
    # One canonical way to do each task an agent needs to play and mod a game.
    "play": (
        "renforge_info",
        "renforge_session",
        "renforge_observe",
        "renforge_act",
        "renforge_explain",
    ),
    # Writing and reloading a mod runs code the agent wrote in the game, so it
    # is its own group: a server limited to `play` cannot run code at all.
    "mod": (
        "renforge_mod",
    ),
    # Earlier live-session tools. Each overlaps the play loop: launch/stop/
    # saves (session), screenshots and listings (observe), clicks, keys,
    # choices, and advancing (act).
    "live": (
        "renforge_launch",
        "renforge_launch_status",
        "renforge_jump",
        "renforge_new_game",
        "renforge_stop",
        "renforge_saves",
        "renforge_advance",
        "renforge_control",
        "renforge_send_input",
        "renforge_list_choices",
        "renforge_select_choice",
        "renforge_list_ui_elements",
        "renforge_click_element",
        "renforge_hover_element",
        "renforge_get_ui_element_bounds",
        "renforge_click_at",
        "renforge_get_displayable_bounds",
        "renforge_hit_test",
        "renforge_screenshot",
        "renforge_capture_screenshot",
        "renforge_diff_screenshots",
        "renforge_find_image_on_screen",
        # Estimates the image shift between two captures, not a language.
        "renforge_estimate_translation",
        "renforge_scene_tree",
        "renforge_measure",
    ),
    # Reading and changing runtime state directly, and scripted runs.
    "debug": (
        "renforge_game_state",
        "renforge_game_state_compact",
        "renforge_inspect_screen",
        "renforge_eval",
        "renforge_set_var",
        "renforge_get_var",
        "renforge_poll_events",
        "renforge_get_errors",
        "renforge_wait_until",
        "renforge_run_scenario",
        "renforge_autopilot",
    ),
    "editor": (
        "renforge_editor",
        "renforge_position_element",
    ),
    "project": (
        "renforge_context",
        "renforge_inspect_image",
        "renforge_inspect_project",
        "renforge_scan_project",
        "renforge_find_references",
        "renforge_parse_lint",
        "renforge_assets",
        "renforge_validate",
    ),
    "translation": (
        "renforge_languages",
        "renforge_translation_stats",
        "renforge_generate_translations",
        "renforge_export_dialogue",
    ),
    "build": (
        "renforge_web_build",
        "renforge_distribute",
    ),
    "docs": (
        "renforge_search_docs",
        "renforge_get_doc",
        "renforge_list_docs",
    ),
}


# Listed by every server: it names the project and the listed groups.
ALWAYS_LISTED: tuple[str, ...] = ("renforge_info",)


def _selected_toolsets(environ: Mapping[str, str] | None) -> list[str]:
    """Parse ``RENFORGE_TOOLSETS`` into group names, in catalogue order.

    Empty or ``all`` selects every group. Raises ``RuntimeError`` for an
    unknown name or a value that names no group (``","``).
    """
    env = os.environ if environ is None else environ
    raw = env.get(TOOLSETS_ENV, "").strip()
    if raw == "":
        return list(TOOLSETS)
    tokens = [part.strip() for part in raw.split(",") if part.strip()]
    if "all" in tokens:
        return list(TOOLSETS)
    unknown = [token for token in tokens if token not in TOOLSETS]
    if unknown or not tokens:
        named = ", ".join(unknown) if unknown else repr(raw)
        raise RuntimeError(
            f"Unknown toolset in {TOOLSETS_ENV}: {named}. "
            f"Toolsets: {', '.join(TOOLSETS)}, all."
        )
    return [name for name in TOOLSETS if name in tokens]


def allowed_tools(known: Iterable[str], environ: Mapping[str, str] | None = None) -> set[str]:
    """Return the tool names the server lists under the current environment.

    Raises ``RuntimeError`` when ``RENFORGE_TOOLSETS`` is invalid.
    """
    names = set(ALWAYS_LISTED)
    for group in _selected_toolsets(environ):
        names |= set(TOOLSETS[group])
    return names & set(known)


def enabled_toolsets(environ: Mapping[str, str] | None = None) -> list[str]:
    """The groups the server lists, in catalogue order (all of them by default).

    Parses the variable exactly as :func:`allowed_tools` does, so it raises
    ``RuntimeError`` for the same invalid values.
    """
    return _selected_toolsets(environ)


# Groups whose tools run arbitrary expressions in the game (renforge_eval,
# renforge_set_var, wait_until and scenario conditions) or that the Live
# Editor drives through the bridge's eval. `mod` runs code through script
# reloads, not the bridge's eval, so the bridge's eval stays off for it.
EVAL_TOOLSETS = frozenset({"debug", "editor"})


def bridge_eval_allowed(environ: Mapping[str, str] | None = None) -> bool:
    """Whether launched games should accept the bridge's eval and set_var.

    A server limited to groups that never evaluate code (``play``, say) starts
    games whose bridge refuses both. With ``play`` alone no tool can run code
    in the game; ``mod`` writes scripts on purpose. An invalid
    ``RENFORGE_TOOLSETS`` fails closed: such a server lists no tools.
    """
    try:
        groups = enabled_toolsets(environ)
    except RuntimeError:
        return False
    return not EVAL_TOOLSETS.isdisjoint(groups)
