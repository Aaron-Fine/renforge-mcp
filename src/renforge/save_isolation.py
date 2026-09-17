"""Application-layer isolation of Ren'Py save and persistent paths.

MCP and dashboard launches default to a disposable save directory so a running
game cannot read or write the user's normal Ren'Py saves (typically
``~/.renpy/<save_directory>``). This is not the Linux Bubblewrap/FUSE
strict-play sandbox: a hostile project can still write to ``$HOME`` if it
bypasses ``config.savedir``. Pass ``savedir=existing`` to use the game's
normal save location.
"""

from __future__ import annotations

import tempfile
from dataclasses import dataclass
from pathlib import Path

EXISTING_TOKENS: frozenset[str] = frozenset({"existing", "default"})
TEMPORARY_TOKENS: frozenset[str] = frozenset({"", "temporary"})
TEMPORARY_PREFIX: str = "renforge-saves-"
MULTIPERSISTENT_DIRNAME: str = "multipersistent"


def classify_savedir(savedir: str | None) -> str:
    """Return ``existing``, ``temporary``, or ``path``.

    ``None`` keeps the low-level launcher on the game's normal save location so
    explicit SDK tests and runners stay unchanged. Empty string and
    ``temporary`` isolate. ``existing`` / ``default`` opt back into user saves.
    """
    if savedir is None:
        return "existing"
    token = str(savedir).strip()
    if token in EXISTING_TOKENS:
        return "existing"
    if token in TEMPORARY_TOKENS:
        return "temporary"
    return "path"


def default_launch_savedir(savedir: str | None) -> str:
    """Coerce omitted MCP/dashboard savedir values to isolated temporary saves."""
    if savedir is None:
        return "temporary"
    token = str(savedir).strip()
    return token if token else "temporary"


@dataclass(frozen=True)
class SaveIsolation:
    """Resolved save location for one launch."""

    mode: str
    savedir: Path | None
    cleanup: bool

    def command_args(self) -> list[str]:
        if self.savedir is None:
            return []
        return ["--savedir", str(self.savedir)]

    def environ(self) -> dict[str, str]:
        if self.savedir is None:
            return {}
        path = str(self.savedir)
        return {
            "RENFORGE_SAVEDIR": path,
            "RENPY_PATH_TO_SAVES": path,
            "RENPY_MULTIPERSISTENT": str(self.savedir / MULTIPERSISTENT_DIRNAME),
        }


def resolve_save_isolation(
    savedir: str | None,
    *,
    cleanup_on_stop: bool = True,
) -> SaveIsolation:
    """Create or select the save directory used by one Ren'Py process."""
    mode = classify_savedir(savedir)
    if mode == "existing":
        return SaveIsolation(mode=mode, savedir=None, cleanup=False)

    if mode == "temporary":
        root = Path(tempfile.mkdtemp(prefix=TEMPORARY_PREFIX))
        cleanup = bool(cleanup_on_stop)
    else:
        assert savedir is not None
        root = Path(savedir).expanduser().resolve()
        root.mkdir(parents=True, exist_ok=True)
        cleanup = False

    (root / MULTIPERSISTENT_DIRNAME).mkdir(parents=True, exist_ok=True)
    return SaveIsolation(mode=mode, savedir=root, cleanup=cleanup)
