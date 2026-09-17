"""Application-layer save isolation: MCP launches must not use user saves."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from renforge.save_isolation import (
    default_launch_savedir,
    classify_savedir,
    resolve_save_isolation,
)


def test_classify_treats_omitted_low_level_savedir_as_existing() -> None:
    assert classify_savedir(None) == "existing"
    assert classify_savedir("existing") == "existing"
    assert classify_savedir("default") == "existing"
    assert classify_savedir(" existing ") == "existing"


def test_classify_isolates_temporary_and_blank_tokens() -> None:
    assert classify_savedir("") == "temporary"
    assert classify_savedir("temporary") == "temporary"
    assert classify_savedir(" temporary ") == "temporary"


def test_classify_treats_other_values_as_explicit_paths() -> None:
    assert classify_savedir("/tmp/renforge-profile-saves") == "path"
    assert classify_savedir("~/renforge-saves") == "path"


def test_mcp_and_dashboard_default_to_temporary_isolation() -> None:
    assert default_launch_savedir(None) == "temporary"
    assert default_launch_savedir("") == "temporary"
    assert default_launch_savedir("   ") == "temporary"
    assert default_launch_savedir("existing") == "existing"
    assert default_launch_savedir("/opt/saves") == "/opt/saves"


def test_temporary_isolation_is_outside_user_save_canaries(tmp_path: Path) -> None:
    home = tmp_path / "home"
    normal_saves = home / ".renpy" / "renforge-demo"
    normal_saves.mkdir(parents=True)
    canary = normal_saves / "normal_save_canary.save"
    canary.write_text("NORMAL-SAVE-CANARY\n", encoding="utf-8")
    project_saves = tmp_path / "project" / "game" / "saves"
    project_saves.mkdir(parents=True)
    (project_saves / "slot1.save").write_text("PROJECT-SAVE\n", encoding="utf-8")

    isolation = resolve_save_isolation("temporary")
    try:
        assert isolation.mode == "temporary"
        assert isolation.cleanup is True
        assert isolation.savedir is not None
        savedir = isolation.savedir
        assert savedir.is_dir()
        assert (savedir / "multipersistent").is_dir()
        assert savedir != normal_saves
        assert not savedir.is_relative_to(home)
        assert not savedir.is_relative_to(tmp_path / "project")
        env = isolation.environ()
        assert env["RENFORGE_SAVEDIR"] == str(savedir)
        assert env["RENPY_PATH_TO_SAVES"] == str(savedir)
        assert env["RENPY_MULTIPERSISTENT"] == str(savedir / "multipersistent")
        assert env["RENFORGE_SAVEDIR"] != str(normal_saves)
        assert isolation.command_args() == ["--savedir", str(savedir)]
        assert canary.read_text(encoding="utf-8") == "NORMAL-SAVE-CANARY\n"
        assert (project_saves / "slot1.save").read_text(encoding="utf-8") == "PROJECT-SAVE\n"
    finally:
        if isolation.savedir is not None:
            shutil.rmtree(isolation.savedir, ignore_errors=True)


def test_existing_mode_does_not_redirect_saves() -> None:
    isolation = resolve_save_isolation("existing")
    assert isolation.mode == "existing"
    assert isolation.savedir is None
    assert isolation.cleanup is False
    assert isolation.command_args() == []
    assert isolation.environ() == {}


def test_explicit_path_is_created_and_not_removed_on_stop(tmp_path: Path) -> None:
    target = tmp_path / "profile" / "primary-saves"
    isolation = resolve_save_isolation(str(target), cleanup_on_stop=True)
    assert isolation.mode == "path"
    assert isolation.savedir == target.resolve()
    assert isolation.cleanup is False
    assert (target / "multipersistent").is_dir()
    assert isolation.command_args() == ["--savedir", str(target.resolve())]


def test_isolated_env_does_not_rewrite_home(monkeypatch, tmp_path: Path) -> None:
    home = tmp_path / "real-home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    isolation = resolve_save_isolation("temporary")
    try:
        env = isolation.environ()
        assert "HOME" not in env
        assert os.environ["HOME"] == str(home)
        assert isolation.savedir is not None
        assert Path(env["RENFORGE_SAVEDIR"]) != home / ".renpy"
    finally:
        if isolation.savedir is not None:
            shutil.rmtree(isolation.savedir, ignore_errors=True)
