from __future__ import annotations

from pathlib import Path

from renforge.play import preflight


def _project(tmp_path: Path, version: str = "8.2.0.24012702") -> Path:
    root = tmp_path / "game-project"
    (root / "game").mkdir(parents=True)
    (root / "game" / "script.rpy").write_text("label start:\n    return\n")
    (root / "renpy").mkdir()
    (root / "renpy" / "vc_version.py").write_text(f"version = {version!r}\n")
    (root / "lib").mkdir()
    (root / "Game.sh").write_text("#!/bin/sh\n")
    return root


def test_preflight_is_read_only_and_detects_exact_version(tmp_path: Path, monkeypatch) -> None:
    root = _project(tmp_path)
    monkeypatch.setattr(preflight.shutil, "which", lambda name: f"/usr/bin/{name}")
    original_exists = preflight.Path.exists
    monkeypatch.setattr(preflight.Path, "exists", lambda self: True if str(self) == "/dev/fuse" else original_exists(self))
    before = sorted(path.relative_to(root) for path in root.rglob("*"))
    result = preflight.inspect_project(root)
    after = sorted(path.relative_to(root) for path in root.rglob("*"))
    assert result.ok
    assert result.engine_version == "8.2.0.24012702"
    assert result.source_completeness == "unverified"
    assert before == after
    assert not (root / ".renforge").exists()


def test_preflight_rejects_wrong_version_and_ambiguous_launcher(tmp_path: Path, monkeypatch) -> None:
    root = _project(tmp_path, "8.2.1.24030407")
    (root / "Other.sh").write_text("#!/bin/sh\n")
    monkeypatch.setattr(preflight.shutil, "which", lambda name: None)
    result = preflight.inspect_project(root)
    assert not result.ok
    assert "unsupported_engine_version" in result.refusals
    assert "bundled_launcher_ambiguous" in result.refusals
    assert "isolation_backend_unavailable" in result.refusals


def test_preflight_rejects_symlinked_launcher(tmp_path: Path) -> None:
    root = _project(tmp_path)
    outside = tmp_path / "outside.sh"
    outside.write_text("#!/bin/sh\n")
    (root / "Bad.sh").symlink_to(outside)
    result = preflight.inspect_project(root, "Bad.sh")
    assert "unsafe_launcher" in result.refusals
