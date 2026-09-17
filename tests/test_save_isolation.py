"""Application-layer isolation: MCP launches must not use user saves or HOME."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from renforge.save_isolation import (
    apply_launch_isolation_defaults,
    classify_savedir,
    configured_isolation_mode,
    default_launch_savedir,
    resolve_launch_isolation,
    resolve_save_isolation,
)


def _cleanup(isolation) -> None:
    root = isolation.session_root or isolation.savedir
    if root is not None:
        shutil.rmtree(root, ignore_errors=True)


def test_classify_treats_omitted_low_level_savedir_as_existing() -> None:
    assert classify_savedir(None) == "existing"
    assert classify_savedir("existing") == "existing"
    assert classify_savedir("default") == "existing"
    assert classify_savedir(" existing ") == "existing"
    assert classify_savedir("auto") == "existing"


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
    assert default_launch_savedir("auto") == "temporary"
    assert default_launch_savedir("   ") == "temporary"
    assert default_launch_savedir("existing") == "existing"
    assert default_launch_savedir("/opt/saves") == "/opt/saves"


def test_apply_defaults_isolate_saves_home_and_preferences() -> None:
    resolved = apply_launch_isolation_defaults()
    assert resolved == {
        "savedir": "temporary",
        "home": "temporary",
        "persistent": "empty",
        "preferences": "empty",
    }


def test_apply_defaults_honor_server_wide_existing_config() -> None:
    resolved = apply_launch_isolation_defaults(
        environ={"RENFORGE_ISOLATION": "existing"}
    )
    assert resolved == {
        "savedir": "existing",
        "home": "existing",
        "persistent": "existing",
        "preferences": "existing",
    }
    assert configured_isolation_mode({"RENFORGE_ISOLATION": "existing"}) == "existing"
    assert configured_isolation_mode({"RENFORGE_ISOLATION": "temporary"}) == "temporary"


def test_savedir_existing_couples_home_unless_home_is_explicit() -> None:
    coupled = apply_launch_isolation_defaults(savedir="existing")
    assert coupled["savedir"] == "existing"
    assert coupled["home"] == "existing"

    isolated_home = apply_launch_isolation_defaults(
        savedir="existing", home="temporary"
    )
    assert isolated_home["home"] == "temporary"

    env_existing = apply_launch_isolation_defaults(
        savedir="temporary",
        environ={"RENFORGE_ISOLATION": "existing"},
    )
    assert env_existing["savedir"] == "temporary"
    assert env_existing["home"] == "temporary"


def test_temporary_isolation_is_outside_user_save_canaries(tmp_path: Path) -> None:
    home = tmp_path / "home"
    normal_saves = home / ".renpy" / "renforge-demo"
    normal_saves.mkdir(parents=True)
    canary = normal_saves / "normal_save_canary.save"
    canary.write_text("NORMAL-SAVE-CANARY\n", encoding="utf-8")
    prefs = home / ".renpy" / "persistent"
    prefs.write_text("HOST-PERSISTENT\n", encoding="utf-8")
    project_saves = tmp_path / "project" / "game" / "saves"
    project_saves.mkdir(parents=True)
    (project_saves / "slot1.save").write_text("PROJECT-SAVE\n", encoding="utf-8")

    isolation = resolve_save_isolation("temporary")
    try:
        assert isolation.mode == "temporary"
        assert isolation.savedir_mode == "temporary"
        assert isolation.home_mode == "temporary"
        assert isolation.cleanup is True
        assert isolation.savedir is not None
        assert isolation.home is not None
        assert isolation.session_root is not None
        savedir = isolation.savedir
        assert savedir.is_dir()
        assert (savedir / "multipersistent").is_dir()
        assert savedir != normal_saves
        assert not savedir.is_relative_to(home)
        assert not savedir.is_relative_to(tmp_path / "project")
        assert isolation.home.is_dir()
        assert isolation.home.is_relative_to(isolation.session_root)
        env = isolation.environ()
        assert env["RENFORGE_SAVEDIR"] == str(savedir)
        assert env["RENPY_PATH_TO_SAVES"] == str(savedir)
        assert env["RENPY_MULTIPERSISTENT"] == str(savedir / "multipersistent")
        assert env["HOME"] == str(isolation.home)
        assert env["XDG_CONFIG_HOME"] == str(isolation.home / ".config")
        assert env["RENFORGE_SAVEDIR"] != str(normal_saves)
        assert isolation.command_args() == ["--savedir", str(savedir)]
        assert canary.read_text(encoding="utf-8") == "NORMAL-SAVE-CANARY\n"
        assert prefs.read_text(encoding="utf-8") == "HOST-PERSISTENT\n"
        assert (project_saves / "slot1.save").read_text(encoding="utf-8") == "PROJECT-SAVE\n"
    finally:
        _cleanup(isolation)


def test_existing_mode_does_not_redirect_saves_or_home() -> None:
    isolation = resolve_save_isolation("existing")
    assert isolation.mode == "existing"
    assert isolation.home_mode == "existing"
    assert isolation.savedir is None
    assert isolation.home is None
    assert isolation.session_root is None
    assert isolation.cleanup is False
    assert isolation.command_args() == []
    assert isolation.environ() == {}


def test_explicit_path_is_created_and_not_removed_on_stop(tmp_path: Path) -> None:
    target = tmp_path / "profile" / "primary-saves"
    isolation = resolve_save_isolation(str(target), cleanup_on_stop=True)
    try:
        assert isolation.mode == "path"
        assert isolation.savedir == target.resolve()
        assert isolation.home_mode == "temporary"
        assert isolation.home is not None
        assert isolation.cleanup is True
        assert (target / "multipersistent").is_dir()
        assert isolation.command_args() == ["--savedir", str(target.resolve())]
    finally:
        _cleanup(isolation)
        assert target.is_dir()
        shutil.rmtree(target, ignore_errors=True)


def test_isolated_env_rewrites_home_and_preserves_xauthority(
    monkeypatch, tmp_path: Path
) -> None:
    home = tmp_path / "real-home"
    home.mkdir()
    xauth = home / ".Xauthority"
    xauth.write_bytes(b"host-xauth")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("XAUTHORITY", raising=False)
    isolation = resolve_launch_isolation(
        "temporary", persistent="empty", preferences="empty", host_env=os.environ
    )
    try:
        env = isolation.environ(host_env=os.environ)
        assert env["HOME"] == str(isolation.home)
        assert isolation.home is not None
        assert Path(env["HOME"]) != home
        assert env["XAUTHORITY"] == str(xauth.resolve())
        assert os.environ["HOME"] == str(home)
        assert isolation.savedir is not None
        assert Path(env["RENFORGE_SAVEDIR"]) != home / ".renpy"
        assert env["RENFORGE_PERSISTENT_MODE"] == "empty"
        assert env["RENFORGE_PREFERENCES_MODE"] == "empty"
    finally:
        _cleanup(isolation)


def test_explicit_home_existing_keeps_host_home_with_isolated_saves() -> None:
    isolation = resolve_launch_isolation("temporary", home="existing")
    try:
        assert isolation.savedir_mode == "temporary"
        assert isolation.home_mode == "existing"
        assert isolation.savedir is not None
        assert isolation.home is None
        assert "HOME" not in isolation.environ()
    finally:
        _cleanup(isolation)


def test_session_init_payload_resets_persistent_and_preferences() -> None:
    from renforge.bridge.artifacts import session_init_payload

    text = session_init_payload().decode("utf-8")
    assert "config.savedir = _renforge_savedir" in text
    assert "config.extra_savedirs = []" in text
    assert "RENFORGE_PERSISTENT_MODE" in text
    assert "unlink('persistent')" in text
    assert "RENFORGE_PREFERENCES_MODE" in text
    assert "persistent._preferences = None" in text
