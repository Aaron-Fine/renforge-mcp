from __future__ import annotations

import ast
import hashlib
import os
import re
import shutil
import stat
from pathlib import Path

from .contracts import SUPPORTED_RENPY_VERSION, PreflightResult

_VERSION_RE = re.compile(r"^\d+\.\d+\.\d+(?:\.\d+)?$")


def project_id(project_root: Path) -> str:
    return hashlib.sha256(os.fsencode(str(project_root))).hexdigest()[:32]


def _contained_regular_file(root: Path, candidate: Path) -> Path | None:
    try:
        if candidate.is_symlink():
            return None
        resolved = candidate.resolve(strict=True)
        mode = resolved.stat().st_mode
    except (OSError, RuntimeError):
        return None
    if not resolved.is_relative_to(root) or not stat.S_ISREG(mode):
        return None
    return resolved


def _static_version(root: Path) -> str | None:
    version_file = _contained_regular_file(root, root / "renpy" / "vc_version.py")
    if version_file is None:
        return None
    try:
        tree = ast.parse(version_file.read_text(encoding="utf-8"), filename=str(version_file))
        for node in tree.body:
            if not isinstance(node, ast.Assign):
                continue
            if any(isinstance(target, ast.Name) and target.id == "version" for target in node.targets):
                value = ast.literal_eval(node.value)
                return value if isinstance(value, str) and _VERSION_RE.fullmatch(value) else None
    except (OSError, UnicodeError, SyntaxError, ValueError):
        return None
    return None


def _backend_available() -> bool:
    return (
        os.name == "posix"
        and all(shutil.which(name) for name in ("bwrap", "fuse-overlayfs", "fusermount3"))
        and Path("/dev/fuse").exists()
    )


def inspect_project(project_path: str | Path, launcher_path: str | Path = "") -> PreflightResult:
    refusals: list[str] = []
    supplied = Path(project_path).expanduser()
    try:
        if supplied.is_symlink():
            refusals.append("project_root_symlink")
        root = supplied.resolve(strict=True)
        if not root.is_dir():
            refusals.append("project_root_not_directory")
    except (OSError, RuntimeError):
        root = supplied.absolute()
        refusals.append("project_root_unavailable")
        return PreflightResult(False, str(root), project_id(root), None, None, 0, "unavailable", tuple(refusals))

    game = root / "game"
    if game.is_symlink() or not game.is_dir():
        refusals.append("unsafe_or_missing_game_directory")
        sources: list[Path] = []
    else:
        try:
            sources = [path for path in game.rglob("*.rpy") if path.is_file() and not path.is_symlink()]
        except OSError:
            sources = []
        if not sources:
            refusals.append("loose_source_required")

    if launcher_path:
        requested = Path(launcher_path)
        if not requested.is_absolute():
            requested = root / requested
        launcher = _contained_regular_file(root, requested)
        if launcher is None:
            refusals.append("unsafe_launcher")
    else:
        candidates = [candidate for candidate in root.glob("*.sh") if _contained_regular_file(root, candidate)]
        launcher = candidates[0].resolve() if len(candidates) == 1 else None
        if len(candidates) == 0:
            refusals.append("bundled_launcher_missing")
        elif len(candidates) > 1:
            refusals.append("bundled_launcher_ambiguous")

    version = _static_version(root)
    if version is None:
        refusals.append("engine_version_unavailable")
    elif version != SUPPORTED_RENPY_VERSION:
        refusals.append("unsupported_engine_version")
    for runtime_path in (root / "renpy", root / "lib"):
        try:
            resolved = runtime_path.resolve(strict=True)
            if runtime_path.is_symlink() or not resolved.is_dir() or not resolved.is_relative_to(root):
                refusals.append("unsafe_runtime_path")
        except (OSError, RuntimeError):
            refusals.append("runtime_path_missing")

    backend_ok = _backend_available()
    if not backend_ok:
        refusals.append("isolation_backend_unavailable")
    return PreflightResult(
        not refusals,
        str(root),
        project_id(root),
        str(launcher) if launcher else None,
        version,
        len(sources),
        "fuse-overlayfs+bubblewrap" if backend_ok else "unavailable",
        tuple(dict.fromkeys(refusals)),
    )
