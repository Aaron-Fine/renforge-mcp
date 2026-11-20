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
import weakref
from pathlib import Path
from typing import Any, Final

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


# Resolved path -> its on-disk spelling. project_state_dir runs on every
# activity entry and every bridge call (an observe poll is one every 0.25 s),
# and spelling a path lists each of its ancestors. Bounded, and cleared when
# full, because a long-lived server may see many paths.
_SPELLINGS: dict[str, Path] = {}
# Case-folded resolved path -> the first answer kept under that folding, so a
# key that changed only in case can still find the answer for its folder.
_SPELLINGS_BY_FOLD: dict[str, Path] = {}
_SPELLINGS_MAX: Final = 1024


def on_disk_spelling(path: Path) -> Path:
    """Spell each component of ``path`` as its directory lists it.

    ``path`` should be absolute and already resolved, as
    ``Path(p).expanduser().resolve()`` gives. This is the one spelling every
    RenForge key for a project folder uses: :func:`project_state_dir` hashes
    it, and :func:`renforge.session_registry.canonical_project_path` keys the
    dashboard and owned-session registries by it.

    On a case-insensitive volume (macOS by default) ``resolve()`` keeps the
    caller's spelling, so ``/Users/me/Game`` and ``/users/me/game`` would get
    two state folders, and two bridge locks, for one directory. A component
    its directory does not list under that spelling takes the listed entry
    that is the same file; on a case-sensitive volume nothing changes.

    An answer is kept for the life of the process once every component was
    found in its directory's listing, so one server keeps one folder (and
    lock) per project even if the folder is renamed while it runs. That is
    the trade-off for a case-only rename on a case-insensitive volume: a
    process started after it spells the folder anew and gets another state
    folder until this one restarts. Spelling afresh here instead would move
    this server off the lock and bridge metadata of the game it is running,
    and that game's lock would still be in the old folder. A path with a
    component that is missing or could not be listed (a folder that does
    not exist yet) is spelled afresh each time.

    On Windows ``resolve()`` itself returns the disk's spelling, so after a
    case-only rename the resolved path, and with it the key, changes. A key
    that differs only in case from one already kept, and names the same
    folder, therefore gets the kept answer too. On a case-sensitive volume
    two such paths are two folders, so each is spelled on its own.
    """
    key = str(path)
    known = _SPELLINGS.get(key)
    if known is not None:
        return known
    folded_key = _fold(key)
    kept = _SPELLINGS_BY_FOLD.get(folded_key)
    if kept is not None and _same_file(kept, path):
        _keep(key, folded_key, kept)
        return kept
    complete = True
    out = Path(path.anchor)
    for name in path.parts[1:]:
        candidate = out / name
        try:
            entries = os.listdir(out)
        except OSError:
            entries = [name]
            complete = False
        if name not in entries:
            folded = _fold(name)
            for entry in entries:
                if _fold(entry) == folded and _same_file(out / entry, candidate):
                    candidate = out / entry
                    break
            else:
                complete = False  # not there (yet): its spelling may change
        out = candidate
    if complete:
        _keep(key, folded_key, out)
    return out


# Spellings a running session's bridge lock, metadata and session key were
# derived from: resolved path -> [folded key, spelling, owner count]. They
# survive eviction, so after a case-only rename the session keeps its keys
# for as long as it runs, however many other paths are spelled meanwhile.
_PINNED: dict[str, list[Any]] = {}


def _keep(key: str, folded_key: str, spelling: Path) -> None:
    if len(_SPELLINGS) >= _SPELLINGS_MAX:
        _SPELLINGS.clear()
        _SPELLINGS_BY_FOLD.clear()
        for pinned_key, (pinned_fold, pinned_spelling, _owners) in _PINNED.items():
            _SPELLINGS[pinned_key] = pinned_spelling
            _SPELLINGS_BY_FOLD.setdefault(pinned_fold, pinned_spelling)
    _SPELLINGS[key] = spelling
    _SPELLINGS_BY_FOLD.setdefault(folded_key, spelling)


def pin_project_spelling(project_root: str | Path, *, owner: object) -> None:
    """Keep the project's current spelling through cache eviction while *owner* lives.

    Call it when a session starts: its keys (state folder, bridge lock,
    session registry) then stay the ones it started with. The pin is dropped
    when *owner* is garbage-collected; an owner that cannot be weakly
    referenced pins nothing.
    """
    key = str(Path(project_root).expanduser().resolve())
    spelling = on_disk_spelling(Path(key))
    try:
        weakref.finalize(owner, _unpin, key)
    except TypeError:
        return
    entry = _PINNED.get(key)
    if entry is None:
        _PINNED[key] = [_fold(key), spelling, 1]
    else:
        entry[2] += 1


def _unpin(key: str) -> None:
    entry = _PINNED.get(key)
    if entry is None:
        return
    entry[2] -= 1
    if entry[2] <= 0:
        del _PINNED[key]


def project_state_dir(project_root: str | Path) -> Path:
    """Return (without creating) the state folder for one project.

    The name keeps the folder recognisable; the hash of the canonical path,
    spelled as the disk lists it, keeps two projects with the same name apart
    and gives every spelling of one directory the same folder.
    """
    root = on_disk_spelling(Path(project_root).expanduser().resolve())
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
