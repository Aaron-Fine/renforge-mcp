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
import unicodedata
from pathlib import Path
from typing import Final

from .util.files import ensure_nofollow_directory

STATE_DIR_ENV: Final = "RENFORGE_STATE_DIR"


def state_root() -> Path:
    """Return the resolved folder that holds every project's state (not created here)."""
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


def _fold(name: str) -> str:
    return unicodedata.normalize("NFC", name).casefold()


def _same_file(a: Path, b: Path) -> bool:
    try:
        return os.path.samefile(a, b)
    except OSError:
        return False


def _on_disk_spelling(path: Path) -> Path:
    """Spell each component of ``path`` as its directory lists it.

    On a case-insensitive volume (macOS by default) ``resolve()`` keeps the
    caller's spelling, so ``/Users/me/Game`` and ``/users/me/game`` would get
    two state folders, and two bridge locks, for one directory. A component
    its directory does not list under that spelling takes the listed entry
    that is the same file; on a case-sensitive volume nothing changes.
    """
    out = Path(path.anchor)
    for name in path.parts[1:]:
        candidate = out / name
        try:
            entries = os.listdir(out)
        except OSError:
            entries = [name]
        if name not in entries:
            folded = _fold(name)
            for entry in entries:
                if _fold(entry) == folded and _same_file(out / entry, candidate):
                    candidate = out / entry
                    break
        out = candidate
    return out


def project_state_dir(project_root: str | Path) -> Path:
    """Return (without creating) the state folder for one project.

    The name keeps the folder recognisable; the hash of the canonical path,
    spelled as the disk lists it, keeps two projects with the same name apart
    and gives every spelling of one directory the same folder.
    """
    root = _on_disk_spelling(Path(project_root).expanduser().resolve())
    digest = hashlib.sha256(str(root).encode("utf-8")).hexdigest()[:16]
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", root.name).strip("._")[:48] or "project"
    return state_root() / "projects" / f"{name}-{digest}"


def ensure_project_state_dir(project_root: str | Path, *parts: str) -> Path:
    """Create (if needed) and return the project's state folder, or a folder in it.

    No component may be a symlink or reparse point: a link planted in the
    state tree must not redirect RenForge's writes. Raises
    :class:`~renforge.util.files.PrivatePathError` when one is. It makes no
    folder private; callers that need that check permissions themselves.
    """
    return ensure_nofollow_directory(project_state_dir(project_root).joinpath(*parts))


def control_dir(project_root: str | Path) -> Path:
    """Private folder holding the bridge lock, bridge metadata, and artifacts."""
    return project_state_dir(project_root) / "control"
