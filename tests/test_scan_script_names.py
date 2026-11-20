"""Which files the project scans treat as scripts, and what an unreadable folder does.

Ren'Py loads a script only when its name ends in lower-case ``.rpy``
(``renpy/script.py`` ``classify_script_files`` uses ``fn.endswith(".rpy")`` on
the name as listed, on every OS), so ``Other.RPY`` is not part of the game.
pathlib's ``glob`` matched ``*.rpy`` case-insensitively on Windows, so the
scans that used it reported such a file there; the extension test fails only
on Windows against the code before the scans moved to ``walk_nofollow``.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from renforge.scanner import scan_project
from renforge.symbols import find_references
from renforge.translation import list_translation_strings
from renforge.ui.graph import _story_map_signature
from renforge.ui.server import _list_script_files

UNLOADED = "unloaded_token"


@pytest.fixture
def project(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    game = root / "game"
    game.mkdir(parents=True)
    (game / "script.rpy").write_text("label start:\n    $ visible = 1\n    return\n", encoding="utf-8")
    (game / "Other.RPY").write_text(f"label unloaded:\n    $ {UNLOADED} = 1\n", encoding="utf-8")
    tl = game / "tl" / "french"
    tl.mkdir(parents=True)
    (tl / "Strings.RPY").write_text(
        f'translate french strings:\n    old "{UNLOADED}"\n    new "{UNLOADED}"\n', encoding="utf-8"
    )
    return root


def test_scans_report_only_the_scripts_renpy_loads(project: Path) -> None:
    result = scan_project(str(project))
    assert [item["file"] for item in result["files"]] == ["game/script.rpy"]
    assert find_references(project, UNLOADED)["occurrences"] == []
    assert UNLOADED not in json.dumps(list_translation_strings(project, "french"))
    assert _list_script_files(project)["files"] == ["game/script.rpy"]
    assert [Path(entry[0]).as_posix() for entry in _story_map_signature(project)[0]] == ["game/script.rpy"]


@pytest.mark.skipif(os.name == "nt" or os.geteuid() == 0, reason="needs POSIX permissions that bind")
def test_the_dashboard_script_list_skips_an_unreadable_folder(project: Path) -> None:
    """pathlib's rglob skipped it too (3.11 drops PermissionError, 3.12+ any OSError)."""
    locked = project / "game" / "locked"
    locked.mkdir()
    (locked / "hidden.rpy").write_text("label hidden:\n    return\n", encoding="utf-8")
    locked.chmod(0)
    try:
        assert _list_script_files(project) == {"ok": True, "files": ["game/script.rpy"]}
    finally:
        locked.chmod(0o700)
