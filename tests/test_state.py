"""Per-project state folders: one folder, and one bridge lock, per real directory."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest

from renforge import state


@pytest.fixture(autouse=True)
def _state_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(state.STATE_DIR_ENV, str(tmp_path / "state"))


def test_an_exact_spelling_keeps_its_existing_folder_name(tmp_path: Path) -> None:
    project = tmp_path / "Games" / "Foo"
    project.mkdir(parents=True)
    digest = hashlib.sha256(str(project.resolve()).encode("utf-8")).hexdigest()[:16]
    assert state.project_state_dir(project).name == f"Foo-{digest}"


def test_another_case_spelling_of_one_directory_shares_its_folder(tmp_path: Path, monkeypatch) -> None:
    """A case-insensitive volume (macOS) answers to any spelling of a name."""
    project = tmp_path / "Games" / "Foo"
    project.mkdir(parents=True)
    real = os.path.samefile

    def case_insensitive(a, b):
        a, b = str(a), str(b)
        if a.casefold() == b.casefold() and a.casefold().startswith(str(tmp_path).casefold()):
            return True
        return real(a, b)

    monkeypatch.setattr(state.os.path, "samefile", case_insensitive)
    assert state.project_state_dir(tmp_path / "games" / "FOO") == state.project_state_dir(project)
    assert state.control_dir(tmp_path / "GAMES" / "foo") == state.control_dir(project)


def test_case_sensitive_siblings_keep_separate_folders(tmp_path: Path) -> None:
    upper = tmp_path / "Game"
    lower = tmp_path / "game"
    upper.mkdir()
    if lower.exists():
        pytest.skip("this volume ignores case")
    lower.mkdir()
    assert state.project_state_dir(upper) != state.project_state_dir(lower)
