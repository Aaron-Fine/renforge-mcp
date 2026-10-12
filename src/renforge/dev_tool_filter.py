"""Development-only switch for which MCP tools the server lists.

FORK-ONLY: this exists to keep agent sessions focused while the fork is
developed, and is meant to be removed before changes go upstream. Removing it
means deleting this module, its one call in ``ToolRegistrar.register``, and
``tests/test_dev_tool_filter.py``.

``RENFORGE_TOOLS``: comma-separated tool names (``renforge_`` prefix optional)
or profiles. Unset, empty, or ``all`` lists every tool.
``RENFORGE_TOOLS_EXCLUDE``: tool names or profiles to hide afterwards.
Unknown names fail at startup so a typo cannot silently hide a tool.
"""

from __future__ import annotations

import os
from collections.abc import Iterable

TOOLS_ENV = "RENFORGE_TOOLS"
EXCLUDE_ENV = "RENFORGE_TOOLS_EXCLUDE"

PROFILES: dict[str, tuple[str, ...]] = {
    # What the playthroughs actually used: start, look, act, progress, save,
    # read the game's code, mod it, stop.
    "play": (
        "renforge_info",
        "renforge_launch",
        "renforge_launch_status",
        "renforge_observe",
        "renforge_act",
        "renforge_advance_until",
        "renforge_saves",
        "renforge_explain",
        "renforge_mod",
        "renforge_stop",
    ),
}


def _expand(raw: str, known: set[str]) -> set[str]:
    names: set[str] = set()
    unknown: list[str] = []
    for token in (part.strip() for part in raw.split(",")):
        if not token:
            continue
        if token == "all":
            names |= known
        elif token in PROFILES:
            names |= set(PROFILES[token])
        else:
            name = token if token.startswith("renforge_") else f"renforge_{token}"
            if name in known:
                names.add(name)
            else:
                unknown.append(token)
    if unknown:
        raise RuntimeError(
            f"Unknown tool or profile in {TOOLS_ENV}/{EXCLUDE_ENV}: {', '.join(unknown)}. "
            f"Profiles: {', '.join(sorted(PROFILES))}, all."
        )
    return names


def allowed_tools(known: Iterable[str], environ: dict[str, str] | None = None) -> set[str]:
    """Return the tool names to register under the current environment."""
    env = os.environ if environ is None else environ
    known_set = set(known)
    include = env.get(TOOLS_ENV, "").strip()
    allowed = known_set if include in ("", "all") else _expand(include, known_set)
    exclude = env.get(EXCLUDE_ENV, "").strip()
    if exclude:
        allowed -= _expand(exclude, known_set)
    return allowed
