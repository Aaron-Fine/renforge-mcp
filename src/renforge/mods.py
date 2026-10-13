"""Write, list, lint, package, and remove mods under ``game/mods/<name>/``.

A mod is a directory the agent owns. Every file it writes is recorded in
``renforge-mod.json`` so that removal can delete exactly what RenForge created
and nothing else. Game files outside ``game/mods/`` are never touched.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Callable

from .lint import run_lint
from .util.files import PrivatePathError, ensure_nofollow_directory, write_atomic
from .state import project_state_dir

MANIFEST_NAME = "renforge-mod.json"
ALLOWED_SUFFIXES = (".rpy", ".txt", ".json", ".md")
MAX_CONTENT_BYTES = 1_000_000
GUIDE = """\
# Modding a Ren'Py game with RenForge

Never edit the game's own files or archives. Put every change in
`game/mods/<name>/` with `renforge_mod(action="write")`. Ren'Py loads every
`.rpy` under `game/` automatically, so nothing else needs registering.

## Loop

1. Find what to change: `renforge_observe` shows each control's `source`
   (`file:line`). `renforge_explain` with a control `id`, a `screen`, or a
   `label` shows the statement, its `action` source, and the `if` conditions
   around it. This works even when the game ships only `.rpa`/`.rpyc`.
2. Write the mod: `renforge_mod(action="write", name=..., path="main.rpy", content=...)`.
3. `renforge_mod(action="reload")` hot-reloads the running game and returns
   errors and a fresh observation. `action="lint"` reports problems in mod files only.
4. Check the result with `renforge_observe` / `renforge_act`.
5. `renforge_mod(action="remove")` deletes exactly the mod's files, including
   compiled `.rpyc` (a leftover `.rpyc` would keep loading), then reload.

## Idioms

- **Run after the game:** start each mod file with `init offset = 999`. Your
  `screen`, `define`, `image`, and `transform` statements then run after the
  game's and replace them.
- **Replace a screen:** define a screen with the same name (and variant) in
  that file. Copy the original from `renforge_explain(screen=...)` and change
  it. There is no screen inheritance.
- **Add always-on UI:** define your own screen and
  `init python: config.overlay_screens.append("mymod_hud")`.
- **Redirect a label:** `init python: config.label_overrides["original"] = "mymod_original"`.
  Jumps and calls to `original` now go to your label. A redirected label cannot
  jump back to the original name.
- **Observe label entry:** Ren'Py 8.1+ has `config.label_callbacks` (a list of
  `fn(name, abnormal)`); 8.0 has only `config.label_callback` (a single function,
  so chain any existing one).
- **New state:** `default mymod_flag = False` is saved and also applied to old
  saves that lack it. `define` is for constants and is not saved. Prefix every
  name with the mod name to avoid collisions.
- **New game / load hooks:** `config.start_callbacks` and
  `config.after_load_callbacks` (lists of zero-argument functions).

## Safety

- Test from the player's real progress with a copy:
  `renforge_session(action="import", slot=...)` (`list` shows `player_slots`),
  then `load` it. The player's own save files are only read.
- A save made while inside a mod label may not load after the mod is removed.
- Removing a mod and reloading restores the original game only when the mod
  wrote nothing outside `game/mods/<name>/`.
"""

_NAME_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")


def _check_name(name: Any) -> str:
    if not isinstance(name, str) or _NAME_RE.fullmatch(name) is None:
        raise ValueError(
            "mod name must match ^[a-z0-9][a-z0-9_-]{0,63}$ (lowercase letters, digits, '_' or '-')"
        )
    return name


def _game_dir(project_root: str | Path) -> Path:
    return Path(project_root).expanduser() / "game"


def _mod_dir(project_root: str | Path, name: str) -> Path:
    return _game_dir(project_root) / "mods" / name


def _validate_relpath(path: Any) -> str:
    """Return ``path`` if it is a safe relative POSIX path inside a mod dir."""
    if not isinstance(path, str) or not path:
        raise ValueError("path must be a non-empty string")
    if "\\" in path:
        raise ValueError("path must use forward slashes, not backslashes")
    if PurePosixPath(path).is_absolute() or PureWindowsPath(path).drive:
        raise ValueError("path must be relative to the mod directory")
    parts = path.split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise ValueError("path must not contain empty, '.', or '..' components")
    if path == MANIFEST_NAME:
        raise ValueError(f"{MANIFEST_NAME} is managed by RenForge and cannot be written directly")
    if PurePosixPath(path).suffix not in ALLOWED_SUFFIXES:
        raise ValueError(f"path suffix must be one of {', '.join(ALLOWED_SUFFIXES)}")
    return path


def _resolve_target(project_root: str | Path, name: str, rel: str) -> Path:
    """Resolve ``rel`` under the mod dir, refusing symlinks under ``game/``."""
    game = _game_dir(project_root)
    mod_dir = _mod_dir(project_root, name)
    chain = [game, game / "mods", mod_dir]
    current = mod_dir
    for part in rel.split("/"):
        current = current / part
        chain.append(current)
    for component in chain:
        if component.is_symlink():
            raise ValueError(f"refusing to use symlink under game/: {component.name}")
    target = mod_dir / rel
    if not target.resolve().is_relative_to(mod_dir.resolve()):
        raise ValueError("resolved path escapes the mod directory")
    return target


def _load_manifest(mod_dir: Path) -> dict[str, Any] | None:
    manifest = mod_dir / MANIFEST_NAME
    if not manifest.is_file():
        return None
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"{MANIFEST_NAME} is unreadable: {exc}") from exc
    files = data.get("files") if isinstance(data, dict) else None
    if not isinstance(files, list) or not all(isinstance(item, str) for item in files):
        raise ValueError(f"{MANIFEST_NAME} has no valid 'files' list")
    return data


def _atomic_write_bytes(target: Path, data: bytes) -> None:
    """Write through no-follow directory handles so a swapped symlink cannot redirect it."""
    try:
        ensure_nofollow_directory(target.parent)
        write_atomic(target, data, follow_symlinks=False)
    except (PrivatePathError, OSError) as exc:
        raise ValueError(f"refusing unsafe write to {target.name}: {exc}") from exc


def _write_manifest(mod_dir: Path, name: str, files: list[str]) -> None:
    updated = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    payload = {"name": name, "files": files, "updated": updated}
    data = (json.dumps(payload, indent=2) + "\n").encode("utf-8")
    _atomic_write_bytes(mod_dir / MANIFEST_NAME, data)


def _files_on_disk(mod_dir: Path) -> list[str]:
    """Return every regular file under the mod dir as a sorted POSIX path."""
    found: list[str] = []
    if not mod_dir.is_dir():
        return found
    for root, dirnames, filenames in os.walk(mod_dir):
        dirnames.sort()
        for filename in filenames:
            full = Path(root) / filename
            if full.is_symlink() or not full.is_file():
                continue
            found.append(full.relative_to(mod_dir).as_posix())
    return sorted(found)


def _unlisted(mod_dir: Path, listed: set[str]) -> list[str]:
    return [rel for rel in _files_on_disk(mod_dir) if rel != MANIFEST_NAME and rel not in listed]


def write_file(project_root: str | Path, name: str, path: str, content: str) -> dict[str, Any]:
    """Write ``content`` to ``game/mods/<name>/<path>`` and record it in the manifest."""
    _check_name(name)
    rel = _validate_relpath(path)
    if not isinstance(content, str):
        raise ValueError("content must be a string")
    data = content.encode("utf-8")
    if len(data) > MAX_CONTENT_BYTES:
        raise ValueError(f"content exceeds {MAX_CONTENT_BYTES} bytes")
    if not _game_dir(project_root).is_dir():
        raise ValueError("project has no game/ directory")

    target = _resolve_target(project_root, name, rel)
    mod_dir = _mod_dir(project_root, name)
    manifest = _load_manifest(mod_dir) if mod_dir.is_dir() else None
    listed = set(manifest["files"]) if manifest else set()
    listed.add(rel)

    _atomic_write_bytes(target, data)
    files = sorted(listed)
    _write_manifest(mod_dir, name, files)
    return {
        "ok": True,
        "name": name,
        "path": f"game/mods/{name}/{rel}",
        "bytes": len(data),
        "files": files,
    }


def list_mods(project_root: str | Path) -> dict[str, Any]:
    """Describe every mod under ``game/mods/`` that has a manifest."""
    mods_dir = _game_dir(project_root) / "mods"
    mods: list[dict[str, Any]] = []
    if mods_dir.is_dir():
        for child in sorted(mods_dir.iterdir()):
            if child.is_symlink() or not child.is_dir():
                continue
            manifest = _load_manifest(child)
            if manifest is None:
                continue
            files = sorted(manifest["files"])
            missing = [rel for rel in files if not (child / rel).is_file()]
            unlisted = [
                rel for rel in _unlisted(child, set(files)) if not rel.endswith(".rpyc")
            ]
            mods.append(
                {"name": child.name, "files": files, "missing": missing, "unlisted": unlisted}
            )
    return {"ok": True, "mods": mods}


def remove(project_root: str | Path, name: str) -> dict[str, Any]:
    """Delete a mod's listed files, their ``.rpyc`` siblings, and its manifest.

    Unlisted files are never deleted; they are reported under ``kept``.
    """
    _check_name(name)
    mod_dir = _mod_dir(project_root, name)
    manifest = _load_manifest(mod_dir) if mod_dir.is_dir() else None
    if manifest is None:
        return {"ok": False, "error": f"unknown mod: {name}"}

    files = sorted(manifest["files"])
    removed: list[str] = []
    kept: list[str] = []
    for rel in files:
        try:
            target = _resolve_target(project_root, name, _validate_relpath(rel))
        except ValueError:
            kept.append(rel)
            continue
        if target.is_symlink():
            kept.append(rel)
            continue
        if target.is_file() and target.resolve().is_relative_to(mod_dir.resolve()):
            target.unlink()
            removed.append(rel)
        if rel.endswith(".rpy"):
            compiled_rel = rel[: -len(".rpy")] + ".rpyc"
            compiled = mod_dir / compiled_rel
            if compiled.is_file() and not compiled.is_symlink():
                compiled.unlink()
                removed.append(compiled_rel)

    (mod_dir / MANIFEST_NAME).unlink()
    kept.extend(_unlisted(mod_dir, set(files)))

    for root, dirnames, _filenames in os.walk(mod_dir, topdown=False):
        for dirname in dirnames:
            try:
                os.rmdir(Path(root) / dirname)
            except OSError:
                pass
    try:
        mod_dir.rmdir()
    except OSError:
        pass
    return {"ok": True, "removed": removed, "kept": sorted(set(kept))}


def package(project_root: str | Path, name: str) -> dict[str, Any]:
    """Zip a mod's listed files into ``<project state>/mods/<name>.zip``."""
    _check_name(name)
    mod_dir = _mod_dir(project_root, name)
    manifest = _load_manifest(mod_dir) if mod_dir.is_dir() else None
    if manifest is None:
        return {"ok": False, "error": f"unknown mod: {name}"}

    candidates = sorted(
        rel for rel in manifest["files"] if not rel.endswith(".rpyc") and rel != MANIFEST_NAME
    )
    out_dir = project_state_dir(project_root) / "mods"
    out_dir.mkdir(parents=True, exist_ok=True)
    zip_path = out_dir / f"{name}.zip"
    fd, tmp = tempfile.mkstemp(dir=str(out_dir), prefix=f".{name}.", suffix=".zip.tmp")
    os.close(fd)
    packed: list[str] = []
    try:
        with zipfile.ZipFile(tmp, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for rel in candidates:
                source = _resolve_target(project_root, name, _validate_relpath(rel))
                if not source.is_file() or source.is_symlink():
                    continue
                archive.write(source, arcname=f"game/mods/{name}/{rel}")
                packed.append(rel)
        os.replace(tmp, zip_path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return {"ok": True, "path": str(zip_path), "files": packed}


def _diagnostic_in_mod(diagnostic: Any, name: str) -> bool:
    if not isinstance(diagnostic, dict):
        return False
    file_path = str(diagnostic.get("file") or "").replace("\\", "/")
    return re.search(rf"(^|/)mods/{re.escape(name)}/", file_path) is not None


def lint(
    project_root: str | Path,
    name: str,
    run: Callable[..., dict[str, Any]] = run_lint,
) -> dict[str, Any]:
    """Run the project lint and keep only diagnostics that point into the mod."""
    _check_name(name)
    result = run(project_root)
    if not isinstance(result, dict):
        result = {"ok": False, "error": "lint returned a non-object result"}
    raw = result.get("diagnostics") or []
    kept = [item for item in raw if _diagnostic_in_mod(item, name)]
    out: dict[str, Any] = {
        "ok": bool(result.get("ok")) and not kept,
        "diagnostics": kept,
        "returncode": result.get("returncode"),
    }
    if result.get("error"):
        out["error"] = result["error"]
    return out
