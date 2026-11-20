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

from .lint import parse_lint_output
from .util.files import (
    PrivatePathError,
    ensure_nofollow_directory,
    is_link,
    read_text_nofollow,
    receipt_path,
    walk_nofollow,
    write_atomic,
)
from .state import ensure_project_state_dir

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
    # Resolve the root once: a symlink above the project (a linked games
    # library, macOS /var, /home -> /var/home) is the user's own layout, and
    # the no-follow write path refuses every link it meets. Links under game/
    # are still refused.
    return Path(project_root).expanduser().resolve() / "game"


def _is_link(path: Path) -> bool:
    """True for a symlink or, on Windows, a junction or other reparse point."""
    try:
        return is_link(path)
    except PrivatePathError:
        return True


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
    # Any case and any folder: NTFS and APFS would map another spelling of
    # the top-level name onto the real manifest.
    if any(part.casefold() == MANIFEST_NAME for part in parts):
        raise ValueError(f"{MANIFEST_NAME} is managed by RenForge and cannot be written directly")
    if PurePosixPath(path).suffix not in ALLOWED_SUFFIXES:
        raise ValueError(f"path suffix must be one of {', '.join(ALLOWED_SUFFIXES)}")
    return path


def _resolve_target(project_root: str | Path, name: str, rel: str) -> Path:
    """Resolve ``rel`` under the mod dir, refusing symlinks under ``game/``.

    ``game``, ``game/mods`` and the mod folder are checked by
    :func:`_refuse_linked_mod_dir`; each component of ``rel`` is checked here.
    """
    _refuse_linked_mod_dir(project_root, name)
    mod_dir = _mod_dir(project_root, name)
    current = mod_dir
    for part in rel.split("/"):
        current = current / part
        if _is_link(current):
            raise ValueError(f"refusing to use symlink under game/: {current.name}")
    target = mod_dir / rel
    if not target.resolve().is_relative_to(mod_dir.resolve()):
        raise ValueError("resolved path escapes the mod directory")
    return target


def _is_safe_relpath(path: str) -> bool:
    try:
        _validate_relpath(path)
    except ValueError:
        return False
    return True


def _refuse_linked_mod_dir(project_root: str | Path, name: str) -> None:
    """Refuse a mod whose ``game``, ``game/mods`` or own folder is a link.

    Through a link, deleting the manifest or empty folders would act on a
    folder outside ``game/mods/<name>/`` while the receipt names the inside.
    """
    game = _game_dir(project_root)
    for component in (game, game / "mods", _mod_dir(project_root, name)):
        if _is_link(component):
            raise ValueError(f"refusing to use symlink under game/: {component.name}")


def _load_manifest(mod_dir: Path) -> dict[str, Any] | None:
    manifest = mod_dir / MANIFEST_NAME
    if _is_link(manifest) or not manifest.is_file():
        return None
    try:
        data = json.loads(read_text_nofollow(manifest))
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
    for root, _dirnames, filenames in walk_nofollow(mod_dir):
        for filename in filenames:
            full = Path(root) / filename
            if _is_link(full) or not full.is_file():
                continue
            found.append(full.relative_to(mod_dir).as_posix())
    return sorted(found)


def _unlisted(mod_dir: Path, listed: set[str]) -> list[str]:
    return [rel for rel in _files_on_disk(mod_dir) if rel != MANIFEST_NAME and rel not in listed]


def _compiled_sibling(rel: str) -> str | None:
    return rel[: -len(".rpy")] + ".rpyc" if rel.endswith(".rpy") else None


def _refuse_foreign_files(mod_dir: Path, name: str, rel: str, manifest: dict[str, Any] | None) -> None:
    """Refuse a write that would take over a file RenForge did not create.

    ``remove`` deletes every listed file and a listed ``.rpy``'s ``.rpyc``, so
    listing a file the game shipped (games with their own mod loader use
    ``game/mods/``) would delete it for good.
    """
    if manifest is None:
        if _files_on_disk(mod_dir):
            raise ValueError(
                f"game/mods/{name}/ already holds files RenForge did not write; choose another mod name"
            )
        return
    if rel in manifest["files"]:
        return
    compiled = _compiled_sibling(rel)
    for candidate in (rel, compiled):
        if candidate is not None and os.path.lexists(mod_dir / candidate):
            raise ValueError(
                f"game/mods/{name}/{candidate} exists but RenForge did not write it; "
                "choose another path or another mod name"
            )


def write_file(project_root: str | Path, name: str, path: str, content: str) -> dict[str, Any]:
    """Write ``content`` to ``game/mods/<name>/<path>`` and record it in the manifest.

    Refuses to overwrite or list a file the mod's manifest does not already
    list. ``files_touched`` names the file and the manifest, project-relative.
    """
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
    manifest = _load_manifest(mod_dir)
    _refuse_foreign_files(mod_dir, name, rel, manifest)
    listed = set(manifest["files"]) if manifest else set()
    listed.add(rel)

    files = sorted(listed)
    # Manifest first: if the file write then fails, the manifest lists a
    # missing file, which list reports. The other order could leave an
    # unlisted .rpy that the game loads and remove() never deletes.
    _write_manifest(mod_dir, name, files)
    _atomic_write_bytes(target, data)
    return {
        "ok": True,
        "name": name,
        "path": f"game/mods/{name}/{rel}",
        "bytes": len(data),
        "files": files,
        "files_touched": sorted([f"game/mods/{name}/{rel}", f"game/mods/{name}/{MANIFEST_NAME}"]),
    }


def list_mods(project_root: str | Path) -> dict[str, Any]:
    """Describe every mod under ``game/mods/`` that has a manifest."""
    mods_dir = _game_dir(project_root) / "mods"
    mods: list[dict[str, Any]] = []
    if mods_dir.is_dir():
        for child in sorted(mods_dir.iterdir()):
            if _is_link(child) or not child.is_dir():
                continue
            manifest = _load_manifest(child)
            if manifest is None:
                continue
            files = sorted(manifest["files"])
            # The manifest may come with the game: check only paths inside the mod.
            missing = [rel for rel in files if not _is_safe_relpath(rel) or not (child / rel).is_file()]
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
    Raises ``ValueError`` when ``game``, ``game/mods`` or the mod folder is a
    link, and never enters a linked folder inside the mod. ``files_deleted``
    names every deleted file, manifest included, project-relative.
    """
    _check_name(name)
    _refuse_linked_mod_dir(project_root, name)
    mod_dir = _mod_dir(project_root, name)
    manifest = _load_manifest(mod_dir)
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
        if _is_link(target):
            kept.append(rel)
            continue
        if target.is_file() and target.resolve().is_relative_to(mod_dir.resolve()):
            target.unlink()
            removed.append(rel)
        compiled_rel = _compiled_sibling(rel)
        if compiled_rel is not None:
            compiled = mod_dir / compiled_rel
            if compiled.is_file() and not _is_link(compiled):
                compiled.unlink()
                removed.append(compiled_rel)

    (mod_dir / MANIFEST_NAME).unlink()
    kept.extend(_unlisted(mod_dir, set(files)))

    # Deepest first; linked folders were pruned, so they are never removed.
    folders = [Path(root) / d for root, dirnames, _ in walk_nofollow(mod_dir) for d in dirnames]
    for folder in reversed(folders):
        try:
            os.rmdir(folder)
        except OSError:
            pass
    try:
        mod_dir.rmdir()
    except OSError:
        pass
    deleted = sorted(f"game/mods/{name}/{rel}" for rel in [*removed, MANIFEST_NAME])
    return {"ok": True, "removed": removed, "kept": sorted(set(kept)), "files_deleted": deleted}


def package(project_root: str | Path, name: str) -> dict[str, Any]:
    """Zip a mod's listed files into ``<project state>/mods/<name>.zip``.

    A listed file that is gone or not a valid mod path is skipped and named
    under ``missing``. ``files_touched`` names the zip by the shared receipt
    rule (``util.files.receipt_path``): its absolute POSIX path, since the
    state folder normally sits outside the project.
    """
    _check_name(name)
    mod_dir = _mod_dir(project_root, name)
    manifest = _load_manifest(mod_dir)
    if manifest is None:
        return {"ok": False, "error": f"unknown mod: {name}"}

    candidates = sorted(
        rel for rel in manifest["files"] if not rel.endswith(".rpyc") and rel != MANIFEST_NAME
    )
    try:
        out_dir = ensure_project_state_dir(project_root, "mods")
    except PrivatePathError as exc:
        raise ValueError(f"refusing unsafe package folder: {exc}") from exc
    zip_path = out_dir / f"{name}.zip"
    fd, tmp = tempfile.mkstemp(dir=str(out_dir), prefix=f".{name}.", suffix=".zip.tmp")
    os.close(fd)
    packed: list[str] = []
    missing: list[str] = []
    try:
        with zipfile.ZipFile(tmp, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for rel in candidates:
                try:
                    source = _resolve_target(project_root, name, _validate_relpath(rel))
                except ValueError:
                    # The manifest may come with the game, as list_mods notes.
                    missing.append(rel)
                    continue
                if not source.is_file() or _is_link(source):
                    missing.append(rel)
                    continue
                archive.write(source, arcname=f"game/mods/{name}/{rel}")
                packed.append(rel)
        if not packed:
            os.unlink(tmp)
            return {"ok": False, "error": f"none of mod {name}'s files exist", "missing": missing}
        os.replace(tmp, zip_path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    result: dict[str, Any] = {
        "ok": True,
        "path": str(zip_path),
        "files": packed,
        "files_touched": [receipt_path(project_root, zip_path)],
    }
    if missing:
        result["missing"] = missing
    return result


_RAW_TAIL_CHARS = 2000
# Ren'Py reports a parse error as 'File "game/x.rpy", line 3: expected statement.',
# which parse_lint_output does not turn into a file:line diagnostic.
_PARSE_ERROR_RE = re.compile(r'^\s*File "(?P<file>[^"]+)", line (?P<line>\d+):\s*(?P<message>.*?)\s*$')


def _diagnostic_in_mod(diagnostic: Any, name: str) -> bool:
    if not isinstance(diagnostic, dict):
        return False
    file_path = str(diagnostic.get("file") or "").replace("\\", "/")
    return re.search(rf"(^|/)mods/{re.escape(name)}/", file_path) is not None


def _parse_errors(raw: str) -> list[dict[str, Any]]:
    """Turn Ren'Py's ``File "...", line N: message`` lines into diagnostics."""
    found: list[dict[str, Any]] = []
    for line in raw.splitlines():
        match = _PARSE_ERROR_RE.match(line)
        if match is None:
            continue
        diagnostic: dict[str, Any] = {
            "file": match.group("file"),
            "line": int(match.group("line")),
            "severity": "error",
        }
        if match.group("message"):
            diagnostic["message"] = match.group("message")
        found.append(diagnostic)
    return found


def _shadow_lint(project_root: str | Path) -> dict[str, Any]:
    """Run Ren'Py lint on a clean shadow copy, as ``renforge_validate`` does.

    Linting in place would compile ``.rpyc`` files and write ``game/cache``
    outside the mod. The shadow also never uses an SDK found in the project.
    The SDK is the game's own engine version (``renforge.json`` pin, shipped
    engine, ``script_version.txt``), as for any ``"stable"`` request, or the
    stable SDK when the project names none.
    """
    from .sdk import bundled_engine_version
    from .tools.validate import validate_project

    version = bundled_engine_version(Path(project_root).expanduser().resolve()) or "stable"
    result = validate_project(str(project_root), version=version)
    raw = "\n".join(part for part in (result.get("stdout"), result.get("stderr")) if part)
    out: dict[str, Any] = {
        "ok": result.get("ok") is True,
        "returncode": result.get("returncode"),
        "timed_out": bool(result.get("timed_out")),
        "diagnostics": parse_lint_output(raw),
        "raw": raw,
    }
    if result.get("error"):
        out["error"] = result["error"]
    return out


def lint(
    project_root: str | Path,
    name: str,
    run: Callable[..., dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Lint the project and keep only diagnostics that point into the mod.

    ``run`` defaults to a lint of a shadow copy, so the game folder is never
    written. The project's ``init`` code still runs, as with
    ``renforge_validate``. When the lint fails with nothing in the mod,
    ``raw_tail`` holds the end of Ren'Py's output; ``timed_out`` is passed on.
    """
    _check_name(name)
    if _load_manifest(_mod_dir(project_root, name)) is None:
        return {"ok": False, "error": f"unknown mod: {name}"}
    result = (run or _shadow_lint)(project_root)
    if not isinstance(result, dict):
        result = {"ok": False, "error": "lint returned a non-object result"}
    raw_text = result.get("raw") if isinstance(result.get("raw"), str) else ""
    candidates = list(result.get("diagnostics") or []) + _parse_errors(raw_text)
    kept: list[Any] = []
    for item in candidates:
        if _diagnostic_in_mod(item, name) and item not in kept:
            kept.append(item)
    out: dict[str, Any] = {
        "ok": bool(result.get("ok")) and not kept,
        "diagnostics": kept,
        "returncode": result.get("returncode"),
    }
    if result.get("timed_out"):
        out["timed_out"] = True
    if result.get("error"):
        out["error"] = result["error"]
    if not out["ok"] and not kept and raw_text.strip():
        out["raw_tail"] = raw_text[-_RAW_TAIL_CHARS:]
    return out
