"""Tool groups the server can list, chosen with ``RENFORGE_TOOLSETS``.

``play`` is the canonical agent loop: one tool per task. The other groups keep
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
