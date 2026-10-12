"""Where RenForge keeps per-project state, outside the game folder.

Activity logs, bridge control files, captures, snapshots, and packaged mods
live under one folder per project so RenForge never writes into a game's own
directory (apart from the bridge script it injects for a running session and
mods the agent writes on purpose).

The root is ``RENFORGE_STATE_DIR`` when set, else ``$XDG_STATE_HOME/renforge``
(``~/.local/state/renforge``), or ``%LOCALAPPDATA%\\renforge`` on Windows.
"""

from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path
from typing import Final

STATE_DIR_ENV: Final = "RENFORGE_STATE_DIR"


def state_root() -> Path:
    configured = os.environ.get(STATE_DIR_ENV, "").strip()
    if configured:
        base = Path(configured)
    elif os.name == "nt" and os.environ.get("LOCALAPPDATA"):
        base = Path(os.environ["LOCALAPPDATA"]) / "renforge"
    else:
        xdg = os.environ.get("XDG_STATE_HOME", "").strip()
        base = (Path(xdg) if xdg else Path.home() / ".local" / "state") / "renforge"
    # Resolve once so no-follow checks below never trip on a symlinked home.
    return base.expanduser().resolve()


def project_state_dir(project_root: str | Path) -> Path:
    """Return (without creating) the state folder for one project.

    The name keeps the folder recognisable; the hash of the canonical path
    keeps two projects with the same name apart.
    """
    root = Path(project_root).expanduser().resolve()
    digest = hashlib.sha256(str(root).encode("utf-8")).hexdigest()[:16]
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", root.name).strip("._")[:48] or "project"
    return state_root() / "projects" / f"{name}-{digest}"


def control_dir(project_root: str | Path) -> Path:
    """Private folder holding the bridge lock, bridge metadata, and artifacts."""
    return project_state_dir(project_root) / "control"
