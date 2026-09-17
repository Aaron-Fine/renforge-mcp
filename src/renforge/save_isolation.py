"""Application-layer isolation of Ren'Py saves, preferences, and HOME.

MCP and dashboard launches default to a disposable session root so a running
game cannot read or write the user's normal Ren'Py state (saves, persistent
data, preferences, and ``$HOME/.renpy``). Pass ``savedir=existing`` /
``home=existing`` / ``RENFORGE_ISOLATION=existing`` to use the user's files.

This is not the Linux Bubblewrap/FUSE sandbox. That helper stays test-only
and is not on the MCP launch path.
"""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

EXISTING_TOKENS: frozenset[str] = frozenset({"existing", "default"})
TEMPORARY_TOKENS: frozenset[str] = frozenset({"", "temporary"})
EMPTY_TOKENS: frozenset[str] = frozenset({"empty", "temporary"})
AUTO_TOKENS: frozenset[str] = frozenset({"auto"})
ISOLATION_ENV: str = "RENFORGE_ISOLATION"
SESSION_PREFIX: str = "renforge-session-"
MULTIPERSISTENT_DIRNAME: str = "multipersistent"
SAVES_DIRNAME: str = "saves"
HOME_DIRNAME: str = "home"


def classify_token(value: str | None, *, omitted: str) -> str:
    """Return ``existing``, ``temporary``, ``empty``, or ``path``."""
    if value is None:
        return omitted
    token = str(value).strip()
    if token in AUTO_TOKENS:
        return omitted
    if token in EXISTING_TOKENS:
        return "existing"
    if token in TEMPORARY_TOKENS:
        return "temporary"
    if token == "empty":
        return "empty"
    return "path"


def classify_savedir(savedir: str | None) -> str:
    """Return ``existing``, ``temporary``, or ``path``.

    ``None`` keeps the low-level launcher on the game's normal save location.
    """
    mode = classify_token(savedir, omitted="existing")
    return "temporary" if mode == "empty" else mode


def configured_isolation_mode(environ: Mapping[str, str] | None = None) -> str:
    """Server-wide default: ``temporary`` unless ``RENFORGE_ISOLATION=existing``."""
    source = os.environ if environ is None else environ
    token = str(source.get(ISOLATION_ENV, "temporary") or "temporary").strip()
    if token in EXISTING_TOKENS:
        return "existing"
    return "temporary"


def default_launch_value(
    value: str | None,
    *,
    field: str,
    environ: Mapping[str, str] | None = None,
) -> str:
    """Fill an omitted MCP/dashboard isolation field from configuration."""
    token = None if value is None else str(value).strip()
    if token and token not in AUTO_TOKENS:
        return token
    if configured_isolation_mode(environ) == "existing":
        return "existing"
    if field in {"persistent", "preferences"}:
        return "empty"
    return "temporary"


def default_launch_savedir(
    savedir: str | None,
    environ: Mapping[str, str] | None = None,
) -> str:
    """Coerce omitted MCP/dashboard savedir values to the configured default."""
    return default_launch_value(savedir, field="savedir", environ=environ)


def apply_launch_isolation_defaults(
    *,
    savedir: str | None = None,
    home: str | None = None,
    persistent: str | None = None,
    preferences: str | None = None,
    environ: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Resolve MCP/dashboard isolation knobs.

    Omitted fields follow ``RENFORGE_ISOLATION`` (default ``temporary``).
    ``savedir=existing`` also selects ``home=existing`` unless *home* was set
    explicitly, so user saves under ``~/.renpy`` remain reachable. Explicit
    ``savedir=temporary`` isolates HOME the same way unless *home* is set.
    """
    home_explicit = home is not None and bool(str(home).strip()) and str(home).strip() not in AUTO_TOKENS
    resolved_savedir = default_launch_value(savedir, field="savedir", environ=environ)
    resolved_home = default_launch_value(home, field="home", environ=environ)
    if not home_explicit:
        savedir_mode = classify_savedir(resolved_savedir)
        if savedir_mode == "existing":
            resolved_home = "existing"
        elif savedir_mode == "temporary":
            resolved_home = "temporary"
    return {
        "savedir": resolved_savedir,
        "home": resolved_home,
        "persistent": default_launch_value(
            persistent, field="persistent", environ=environ
        ),
        "preferences": default_launch_value(
            preferences, field="preferences", environ=environ
        ),
    }


@dataclass(frozen=True)
class LaunchIsolation:
    """Resolved save, preference, and HOME locations for one launch."""

    savedir_mode: str
    home_mode: str
    persistent_mode: str
    preferences_mode: str
    savedir: Path | None
    home: Path | None
    session_root: Path | None
    cleanup: bool

    @property
    def mode(self) -> str:
        """Backward-compatible alias for :attr:`savedir_mode`."""
        return self.savedir_mode

    def command_args(self) -> list[str]:
        if self.savedir is None:
            return []
        return ["--savedir", str(self.savedir)]

    def environ(self, host_env: Mapping[str, str] | None = None) -> dict[str, str]:
        env: dict[str, str] = {}
        if self.persistent_mode != "existing":
            env["RENFORGE_PERSISTENT_MODE"] = self.persistent_mode
        if self.preferences_mode != "existing":
            env["RENFORGE_PREFERENCES_MODE"] = self.preferences_mode
        if self.savedir is not None:
            path = str(self.savedir)
            env["RENFORGE_SAVEDIR"] = path
            env["RENPY_PATH_TO_SAVES"] = path
            env["RENPY_MULTIPERSISTENT"] = str(self.savedir / MULTIPERSISTENT_DIRNAME)
        if self.home is not None:
            env.update(_home_environ(self.home, host_env=host_env))
        return env


def resolve_save_isolation(
    savedir: str | None,
    *,
    cleanup_on_stop: bool = True,
) -> LaunchIsolation:
    """Savedir-focused resolver used by the low-level launcher tests."""
    return resolve_launch_isolation(savedir, cleanup_on_stop=cleanup_on_stop)


def resolve_launch_isolation(
    savedir: str | None = None,
    *,
    home: str | None = None,
    persistent: str | None = None,
    preferences: str | None = None,
    cleanup_on_stop: bool = True,
    host_env: Mapping[str, str] | None = None,
) -> LaunchIsolation:
    """Create or select isolated save/home directories for one Ren'Py process.

    Low-level ``savedir=None`` keeps the game's normal save location and host
    HOME. ``savedir=temporary`` also isolates HOME unless *home* is explicit.
    """
    _ = host_env
    savedir_mode = classify_savedir(savedir)
    if home is None:
        home_mode = "temporary" if savedir_mode != "existing" else "existing"
    else:
        home_mode = classify_token(home, omitted="existing")
        if home_mode == "empty":
            home_mode = "temporary"

    persistent_mode = classify_token(persistent, omitted="existing")
    if persistent_mode == "temporary":
        persistent_mode = "empty"
    preferences_mode = classify_token(preferences, omitted="existing")
    if preferences_mode == "temporary":
        preferences_mode = "empty"

    needs_session = savedir_mode == "temporary" or home_mode == "temporary"
    session_root: Path | None = None
    savedir_path: Path | None = None
    home_path: Path | None = None
    cleanup = False

    if needs_session:
        session_root = Path(tempfile.mkdtemp(prefix=SESSION_PREFIX))
        cleanup = bool(cleanup_on_stop)

    if savedir_mode == "temporary":
        assert session_root is not None
        savedir_path = session_root / SAVES_DIRNAME
        savedir_path.mkdir(parents=True, exist_ok=True)
        (savedir_path / MULTIPERSISTENT_DIRNAME).mkdir(parents=True, exist_ok=True)
    elif savedir_mode == "path":
        assert savedir is not None
        savedir_path = Path(savedir).expanduser().resolve()
        savedir_path.mkdir(parents=True, exist_ok=True)
        (savedir_path / MULTIPERSISTENT_DIRNAME).mkdir(parents=True, exist_ok=True)

    if home_mode == "temporary":
        assert session_root is not None
        home_path = session_root / HOME_DIRNAME
        _prepare_isolated_home(home_path)
    elif home_mode == "path":
        assert home is not None
        home_path = Path(home).expanduser().resolve()
        _prepare_isolated_home(home_path)

    return LaunchIsolation(
        savedir_mode=savedir_mode,
        home_mode=home_mode,
        persistent_mode=persistent_mode,
        preferences_mode=preferences_mode,
        savedir=savedir_path,
        home=home_path,
        session_root=session_root,
        cleanup=cleanup,
    )


def _prepare_isolated_home(home: Path) -> None:
    home.mkdir(parents=True, exist_ok=True)
    (home / ".config").mkdir(parents=True, exist_ok=True)
    (home / ".cache").mkdir(parents=True, exist_ok=True)
    (home / ".local" / "share").mkdir(parents=True, exist_ok=True)
    (home / "tmp").mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        (home / "AppData" / "Roaming").mkdir(parents=True, exist_ok=True)
        (home / "AppData" / "Local").mkdir(parents=True, exist_ok=True)


def _home_environ(home: Path, *, host_env: Mapping[str, str] | None) -> dict[str, str]:
    env = {
        "HOME": str(home),
        "XDG_CONFIG_HOME": str(home / ".config"),
        "XDG_CACHE_HOME": str(home / ".cache"),
        "XDG_DATA_HOME": str(home / ".local" / "share"),
        "TMPDIR": str(home / "tmp"),
        "TMP": str(home / "tmp"),
        "TEMP": str(home / "tmp"),
    }
    if os.name == "nt":
        env["USERPROFILE"] = str(home)
        env["APPDATA"] = str(home / "AppData" / "Roaming")
        env["LOCALAPPDATA"] = str(home / "AppData" / "Local")
    env.update(_preserve_x11_auth(host_env or os.environ))
    return env


def _preserve_x11_auth(host_env: Mapping[str, str]) -> dict[str, str]:
    """Keep the host Xauthority file reachable after HOME is replaced."""
    extra: dict[str, str] = {}
    auth = host_env.get("XAUTHORITY")
    if auth:
        auth_path = Path(auth).expanduser()
        if auth_path.is_file():
            extra["XAUTHORITY"] = str(auth_path.resolve())
            return extra
    host_home = host_env.get("HOME")
    if host_home:
        xauth = Path(host_home) / ".Xauthority"
        if xauth.is_file():
            extra["XAUTHORITY"] = str(xauth.resolve())
    return extra
