from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from renforge.play.profiles import ProfileStore


def _project(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    project.mkdir()
    return project


def test_create_list_inspect_private_profile(tmp_path: Path) -> None:
    store = ProfileStore(_project(tmp_path), tmp_path / "state")
    created = store.create("playtest-1")
    profile = store.root / "profiles" / "playtest-1"
    assert created == store.inspect("playtest-1")
    assert store.list() == [created]
    if os.name == "posix":
        assert profile.stat().st_mode & 0o777 == 0o700
        assert (profile / "profile.json").stat().st_mode & 0o777 == 0o600
    assert {path.name for path in profile.iterdir()} == {
        "profile.json", "primary-saves", "game-saves", "multipersistent"
    }


def test_profile_rejects_unsafe_ids_collisions_and_project_state_root(tmp_path: Path) -> None:
    project = _project(tmp_path)
    store = ProfileStore(project, tmp_path / "state")
    for value in ("", "../escape", "a/b", ".hidden"):
        with pytest.raises(ValueError):
            store.create(value)
    store.create("same")
    with pytest.raises(FileExistsError):
        store.create("same")
    with pytest.raises(ValueError, match="outside"):
        ProfileStore(project, project / "state")


def test_list_ignores_symlinked_profile_and_does_not_read_save_payloads(tmp_path: Path) -> None:
    project = _project(tmp_path)
    store = ProfileStore(project, tmp_path / "state")
    store.create("safe")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "profile.json").write_text(json.dumps({"profile_id": "stolen"}))
    (store.root / "profiles" / "linked").symlink_to(outside, target_is_directory=True)
    secret = store.root / "profiles" / "safe" / "primary-saves" / "secret.save"
    secret.write_text("do not parse")
    assert [profile["profile_id"] for profile in store.list()] == ["safe"]
