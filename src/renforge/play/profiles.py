from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any

from .contracts import SCHEMA_VERSION
from .preflight import project_id

_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def default_state_root() -> Path:
    configured = os.environ.get("RENFORGE_PLAY_STATE_ROOT")
    if configured:
        return Path(configured).expanduser()
    base = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state"))
    return base / "renforge" / "play"


def _private_directory(path: Path) -> None:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.is_symlink() or not path.is_dir():
        raise ValueError(f"Unsafe state directory: {path}")
    path.chmod(0o700)


def _write_json(path: Path, value: dict[str, Any]) -> None:
    _private_directory(path.parent)
    fd, raw = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(raw)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, sort_keys=True, separators=(",", ":"))
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        temporary.unlink(missing_ok=True)


class ProfileStore:
    def __init__(self, project_root: Path, state_root: Path | None = None):
        self.project_root = project_root.expanduser().resolve(strict=True)
        self.state_root = (state_root or default_state_root()).expanduser().resolve(strict=False)
        if self.state_root == self.project_root or self.state_root.is_relative_to(self.project_root):
            raise ValueError("Play state root must be outside the project")
        self.project_id = project_id(self.project_root)
        self.root = self.state_root / self.project_id

    def create(self, profile_id: str) -> dict[str, Any]:
        if not _SAFE_ID.fullmatch(profile_id):
            raise ValueError("Profile ID must be a safe 1-64 character component")
        profile = self.root / "profiles" / profile_id
        try:
            profile.mkdir(mode=0o700, parents=True, exist_ok=False)
        except FileExistsError:
            raise FileExistsError(f"Profile already exists: {profile_id}") from None
        for name in ("primary-saves", "game-saves", "multipersistent"):
            (profile / name).mkdir(mode=0o700)
        metadata = {
            "schema_version": SCHEMA_VERSION,
            "project_id": self.project_id,
            "profile_id": profile_id,
            "state": "available",
        }
        _write_json(profile / "profile.json", metadata)
        return metadata

    def inspect(self, profile_id: str) -> dict[str, Any]:
        if not _SAFE_ID.fullmatch(profile_id):
            raise ValueError("Unsafe profile ID")
        metadata = self.root / "profiles" / profile_id / "profile.json"
        if metadata.is_symlink():
            raise ValueError("Unsafe profile metadata")
        return json.loads(metadata.read_text(encoding="utf-8"))

    def list(self) -> list[dict[str, Any]]:
        profiles = self.root / "profiles"
        if not profiles.exists():
            return []
        result = []
        for child in sorted(profiles.iterdir(), key=lambda path: path.name):
            if child.is_symlink() or not child.is_dir() or not _SAFE_ID.fullmatch(child.name):
                continue
            try:
                result.append(self.inspect(child.name))
            except (OSError, ValueError, json.JSONDecodeError):
                continue
        return result
