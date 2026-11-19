"""Project scans never walk into a linked folder, Windows junctions included.

os.walk and pathlib's ``**`` already skip a symlinked folder, but a Windows
junction is not a symlink, so the scans would read the tree it points to.
These tests mark a real folder as a link through util.files.is_link, the
check that also sees junctions, so they run on every platform.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from renforge.assets import analyze_assets
from renforge.scanner import scan_project
from renforge.symbols import find_references
from renforge.translation import list_translation_strings
from renforge.util import files
from renforge.util.files import PrivatePathError

SECRET = "elsewhere_token"


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "project"
    game = root / "game"
    game.mkdir(parents=True)
    (game / "script.rpy").write_text("label start:\n    $ visible = 1\n    return\n", encoding="utf-8")
    junction = game / "junction"
    junction.mkdir()
    (junction / "other.rpy").write_text(
        f'label leaked:\n    $ {SECRET} = 1\n    show {SECRET}\n', encoding="utf-8"
    )
    (junction / "picture.png").write_bytes(b"")
    tl_junction = game / "tl" / "french" / "junction"
    tl_junction.mkdir(parents=True)
    (tl_junction / "strings.rpy").write_text(
        f'translate french strings:\n    old "{SECRET}"\n    new "{SECRET}"\n', encoding="utf-8"
    )
    real = files.is_link
    monkeypatch.setattr(files, "is_link", lambda path: Path(path).name == "junction" or real(path))
    return root


def test_walk_nofollow_prunes_linked_folders(project: Path) -> None:
    seen = [Path(root).name for root, _dirs, _files in files.walk_nofollow(project / "game")]
    assert "junction" not in seen and "french" in seen


def test_walk_nofollow_prunes_a_folder_whose_attributes_cannot_be_read(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "unreadable").mkdir()

    def refuse(_path):
        raise PrivatePathError("PATH_UNVERIFIABLE", "cannot read attributes")

    monkeypatch.setattr(files, "is_link", refuse)
    assert [dirs for _root, dirs, _files in files.walk_nofollow(tmp_path)] == [[]]


def test_scan_does_not_walk_into_a_junction(project: Path) -> None:
    result = scan_project(str(project))
    assert SECRET not in json.dumps(result) and "leaked" not in json.dumps(result)
    assert [item["file"] for item in result["files"]] == ["game/script.rpy"]


def test_assets_do_not_walk_into_a_junction(project: Path) -> None:
    result = analyze_assets(project)
    assert SECRET not in json.dumps(result)
    assert not any("junction" in name for name in result["asset_files"])


def test_find_references_does_not_walk_into_a_junction(project: Path) -> None:
    assert find_references(project, SECRET)["occurrences"] == []
    assert find_references(project, "visible")["pagination"]["returned"] == 1


def test_translation_strings_do_not_walk_into_a_junction(project: Path) -> None:
    assert SECRET not in json.dumps(list_translation_strings(project, "french"))


def test_dashboard_script_list_and_story_map_do_not_walk_into_a_junction(project: Path) -> None:
    from renforge.ui.graph import _story_map_signature
    from renforge.ui.server import _list_script_files

    assert _list_script_files(project)["files"] == ["game/script.rpy"]
    entries = _story_map_signature(project)[0]
    assert not any("junction" in str(entry) for entry in entries)
