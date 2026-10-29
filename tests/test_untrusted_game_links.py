"""A game folder is untrusted: a linked file in it must not make RenForge read
and report a file elsewhere on the machine."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from renforge.assets import analyze_assets
from renforge.mods import list_mods
from renforge.save_isolation import list_host_slots
from renforge.scanner import scan_project
from renforge.symbols import find_references
from renforge.translation import list_translation_strings

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="symlinks need privileges on Windows")

SECRET = "aws_secret_access_key"


@pytest.fixture
def project(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    game = root / "game"
    game.mkdir(parents=True)
    (game / "script.rpy").write_text("label start:\n    $ visible = 1\n    return\n", encoding="utf-8")
    secret = tmp_path / "outside" / "credentials.rpy"
    secret.parent.mkdir()
    secret.write_text(
        f"label leaked:\n    $ {SECRET} = 1\n    image leak = \"{SECRET}.png\"\n"
        f'    show {SECRET}\n',
        encoding="utf-8",
    )
    (game / "notes.rpy").symlink_to(secret)
    return root


def test_find_references_skips_a_linked_script(project: Path) -> None:
    assert find_references(project, SECRET)["occurrences"] == []
    assert find_references(project, "visible")["pagination"]["returned"] == 1


def test_scan_skips_a_linked_script(project: Path) -> None:
    result = scan_project(str(project))
    assert SECRET not in json.dumps(result)
    assert "leaked" not in json.dumps(result)
    assert [item["file"] for item in result["files"]] == ["game/script.rpy"]


def test_assets_skip_a_linked_script(project: Path) -> None:
    result = analyze_assets(project)
    assert SECRET not in json.dumps(result)


def test_translation_strings_skip_a_linked_file(project: Path, tmp_path: Path) -> None:
    tl = project / "game" / "tl" / "french"
    tl.mkdir(parents=True)
    outside = tmp_path / "outside" / "tl.rpy"
    outside.write_text(f'translate french strings:\n    old "{SECRET}"\n    new "x"\n', encoding="utf-8")
    (tl / "linked.rpy").symlink_to(outside)
    assert SECRET not in json.dumps(list_translation_strings(project, "french"))


def test_mod_manifest_entries_outside_the_mod_are_not_probed(project: Path, tmp_path: Path) -> None:
    mod = project / "game" / "mods" / "shipped"
    mod.mkdir(parents=True)
    probe = tmp_path / "outside" / "exists.rpy"
    probe.write_text("", encoding="utf-8")
    (mod / "renforge-mod.json").write_text(
        json.dumps({"files": [str(probe), "../../../../outside/exists.rpy"]}), encoding="utf-8"
    )
    [listed] = list_mods(project)["mods"]
    # Both entries would exist if probed; they are reported missing unchecked.
    assert listed["missing"] == sorted([str(probe), "../../../../outside/exists.rpy"])


def test_a_linked_mod_manifest_is_not_read(project: Path, tmp_path: Path) -> None:
    mod = project / "game" / "mods" / "linked"
    mod.mkdir(parents=True)
    outside = tmp_path / "outside" / "manifest.json"
    outside.write_text(json.dumps({"files": ["a.rpy"]}), encoding="utf-8")
    (mod / "renforge-mod.json").symlink_to(outside)
    assert list_mods(project)["mods"] == []


def test_save_slot_listing_ignores_a_linked_sidecar(project: Path, tmp_path: Path) -> None:
    saves = project / "game" / "saves"
    saves.mkdir()
    secret = tmp_path / "outside" / "secret.json"
    secret.write_text(json.dumps({"_save_name": SECRET}), encoding="utf-8")
    (saves / "1-1-LT1.save").write_bytes(b"SLOT")
    (saves / "1-1-LT1.save.json").symlink_to(secret)
    listed = list_host_slots(project, home=tmp_path / "home")
    assert [item["extra_info"] for item in listed["slots"]] == [""]


def test_a_game_folder_linked_out_of_the_project_is_not_scanned(tmp_path: Path) -> None:
    elsewhere = tmp_path / "home"
    elsewhere.mkdir()
    (elsewhere / "private.rpy").write_text(f"label leaked:\n    $ {SECRET} = 1\n", encoding="utf-8")
    (elsewhere / "tl" / "french").mkdir(parents=True)
    (elsewhere / "tl" / "french" / "x.rpy").write_text(
        f'translate french strings:\n    old "{SECRET}"\n    new "x"\n', encoding="utf-8"
    )
    root = tmp_path / "project"
    root.mkdir()
    (root / "game").symlink_to(elsewhere, target_is_directory=True)

    assert find_references(root, SECRET)["occurrences"] == []
    assert SECRET not in json.dumps(scan_project(str(root)))
    assert SECRET not in json.dumps(analyze_assets(root))
    assert list_translation_strings(root, "french") == []
