"""Tool groups the server can list, chosen with ``RENFORGE_TOOLSETS``.

``play`` is the canonical agent loop: one tool per task. ``mod`` adds
``renforge_mod``, which writes and reloads game code. The other groups keep
every earlier capability available for existing clients, scripts, and the
Live Editor, and are listed unless the server is limited to fewer groups.

``RENFORGE_TOOLSETS``: comma-separated group names, or ``all`` (the default).
Unknown names stop the server at startup so a typo cannot hide tools.
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
    ),
    "translation": (
        "renforge_languages",
        "renforge_translation_stats",
        "renforge_generate_translations",
        "renforge_export_dialogue",
        "renforge_estimate_translation",
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


def allowed_tools(known: Iterable[str], environ: Mapping[str, str] | None = None) -> set[str]:
    """Return the tool names the server lists under the current environment."""
    env = os.environ if environ is None else environ
    known_set = set(known)
    raw = env.get(TOOLSETS_ENV, "").strip()
    if raw in ("", "all"):
        return known_set
    names: set[str] = set()
    unknown: list[str] = []
    for token in (part.strip() for part in raw.split(",")):
        if not token:
            continue
        if token == "all":
            return known_set
        if token in TOOLSETS:
            names |= set(TOOLSETS[token])
        else:
            unknown.append(token)
    if unknown:
        raise RuntimeError(
            f"Unknown toolset in {TOOLSETS_ENV}: {', '.join(unknown)}. "
            f"Toolsets: {', '.join(TOOLSETS)}, all."
        )
    return names & known_set


def enabled_toolsets(environ: Mapping[str, str] | None = None) -> list[str]:
    """The groups the server lists, in catalogue order (all of them by default)."""
    env = os.environ if environ is None else environ
    tokens = {part.strip() for part in env.get(TOOLSETS_ENV, "").split(",") if part.strip()}
    if not tokens or "all" in tokens:
        return list(TOOLSETS)
    return [name for name in TOOLSETS if name in tokens]


# Groups whose tools run arbitrary expressions in the game (renforge_eval,
# renforge_set_var, wait_until and scenario conditions) or that the Live
# Editor drives through the bridge's eval. `mod` runs code through script
# reloads, not the bridge's eval, so the bridge's eval stays off for it.
EVAL_TOOLSETS = frozenset({"debug", "editor"})


def bridge_eval_allowed(environ: Mapping[str, str] | None = None) -> bool:
    """Whether launched games should accept the bridge's eval and set_var.

    A server limited to groups that never evaluate code (``play``, say) starts
    games whose bridge refuses both. With ``play`` alone no tool can run code
    in the game; ``mod`` writes scripts on purpose.
    """
    return not EVAL_TOOLSETS.isdisjoint(enabled_toolsets(environ))
